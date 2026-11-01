import tweepy
import os
import json
import argparse
from dotenv import load_dotenv
import pandas as pd
from pathlib import Path
from time import sleep
from requests.exceptions import ConnectionError, ConnectTimeout

# Load environment variables from .env file
load_dotenv()

bearer_token = os.getenv('BEARER_TOKEN')

# Authenticate using the Bearer Token
client = tweepy.Client(bearer_token=bearer_token, wait_on_rate_limit=True)

BASE_DIR = Path(__file__).resolve().parent

TWEET_FIELDS = ['id', 'text', 'author_id', 'created_at', 'lang', 'public_metrics', 'entities']
USER_FIELDS = ['username', 'name', 'verified', 'public_metrics', 'created_at']

# Asking the API to expand author_id returns the author objects in the same
# response, so no separate per-tweet user lookup is needed.
EXPANSIONS = ['author_id']

# The search endpoint rejects max_results outside this range, so a request for
# fewer than MIN_RESULTS still has to ask for MIN_RESULTS and trim afterwards.
MIN_RESULTS = 10
MAX_RESULTS = 100

DEFAULT_PER_QUERY = 100

# Guards against a typo turning a run into an accidental quota burn, while still
# allowing targets above MAX_RESULTS, which pagination fetches across pages.
MAX_PER_QUERY = 1000

OUTPUT_NAME = 'tweets_all.csv'
STATE_NAME = '.xmsg_state.json'

COLUMNS = [
    'id',
    'matched_queries',
    'username',
    'user_name',
    'user_verified',
    'user_followers',
    'user_created_at',
    'text',
    'created_at',
    'lang',
    'retweet_count',
    'reply_count',
    'like_count',
    'quote_count',
    'hashtags',
]


def users_from_response(response):
    users = {}
    includes = getattr(response, 'includes', None) or {}
    for user in includes.get('users', None) or []:
        users[str(user.id)] = user
    return users


# Function to fetch tweets with retries
def fetch_page(query, max_count, next_token=None, retries=3):
    for i in range(retries):
        try:
            return client.search_recent_tweets(
                query=query,
                max_results=max_count,
                tweet_fields=TWEET_FIELDS,
                user_fields=USER_FIELDS,
                expansions=EXPANSIONS,
                next_token=next_token,
            )
        except (ConnectionError, ConnectTimeout) as connection_error:
            print(f"Connection error: {connection_error}. Retrying ({i+1}/{retries})...")
            sleep(5)
    # Returning None lets the caller skip this query and continue with the rest,
    # instead of raising a bare Exception that no caller handles.
    return None


def fetch_query(query, target, retries=3):
    """Collect up to `target` tweets for one query, following next_token pages."""
    collected = []
    users = {}
    next_token = None
    while len(collected) < target:
        remaining = target - len(collected)
        page_size = max(MIN_RESULTS, min(MAX_RESULTS, remaining))
        response = fetch_page(query, page_size, next_token, retries)
        if response is None or response.data is None:
            break
        collected.extend(response.data)
        users.update(users_from_response(response))
        next_token = (getattr(response, 'meta', None) or {}).get('next_token')
        if not next_token:
            break
    return collected[:target], users


def build_record(tweet, user):
    followers = None
    if user is not None:
        followers = (getattr(user, 'public_metrics', None) or {}).get('followers_count')

    # The raw entities object is a nested dict, which pandas writes as an
    # unparseable Python repr. Keep only the hashtags as a pipe-separated list
    # so the column is usable for analysis.
    entities = tweet.entities or {}
    hashtags = '|'.join(
        tag for tag in (h.get('tag', '') for h in (entities.get('hashtags') or [])) if tag
    )

    return {
        'id': tweet.id,
        'matched_queries': [],
        'username': getattr(user, 'username', None) if user else None,
        'user_name': getattr(user, 'name', None) if user else None,
        'user_verified': getattr(user, 'verified', None) if user else None,
        'user_followers': followers,
        'user_created_at': getattr(user, 'created_at', None) if user else None,
        'text': tweet.text,
        'created_at': tweet.created_at,
        'lang': tweet.lang,
        'retweet_count': tweet.public_metrics['retweet_count'],
        'reply_count': tweet.public_metrics['reply_count'],
        'like_count': tweet.public_metrics['like_count'],
        'quote_count': tweet.public_metrics['quote_count'],
        'hashtags': hashtags,
    }


def write_export(records, path):
    rows = []
    for record in records.values():
        row = dict(record)
        row['matched_queries'] = '|'.join(row.get('matched_queries') or [])
        rows.append(row)
    df = pd.DataFrame(rows, columns=COLUMNS)
    df.to_csv(path, index=False)
    return len(df)


def load_state(path):
    if not os.path.exists(path):
        return set(), {}
    with open(path, encoding='utf-8') as file:
        data = json.load(file)
    return set(data.get('completed_queries') or []), data.get('records') or {}


def save_state(path, completed, records):
    payload = {'completed_queries': sorted(completed), 'records': records}
    temp_path = f'{path}.tmp'
    with open(temp_path, 'w', encoding='utf-8') as file:
        # Datetimes are not JSON serialisable and are stored as ISO strings;
        # CSV output renders them the same way.
        json.dump(payload, file, default=str)
    os.replace(temp_path, path)


def positive_count(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}")
    if not 1 <= number <= MAX_PER_QUERY:
        raise argparse.ArgumentTypeError(
            f"must be between 1 and {MAX_PER_QUERY}, got {number}")
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Collect recent tweets for every term in a query file into one CSV.')
    parser.add_argument(
        '--queries',
        default=str(BASE_DIR / 'queries.txt'),
        help='file with one search term per line (default: queries.txt next to this script)')
    parser.add_argument(
        '--output',
        default=OUTPUT_NAME,
        help=f'CSV file to write (default: {OUTPUT_NAME})')
    parser.add_argument(
        '--tweets-per-query',
        type=positive_count,
        default=DEFAULT_PER_QUERY,
        metavar='N',
        help=f'tweets to collect per term (default: {DEFAULT_PER_QUERY})')
    parser.add_argument(
        '--state',
        default=STATE_NAME,
        help=f'checkpoint file used to resume an interrupted run (default: {STATE_NAME})')
    parser.add_argument(
        '--restart',
        action='store_true',
        help='ignore any existing checkpoint and collect everything again')
    return parser.parse_args(argv)


# Read queries from the text file next to this script, so the tool works
# regardless of the directory it is launched from.
def load_queries(path=None):
    query_file = Path(path) if path else BASE_DIR / 'queries.txt'
    with open(query_file, encoding='utf-8') as file:
        return [line.strip() for line in file if line.strip()]

# Main Function
def main(argv=None):
    args = parse_args(argv)
    queries = load_queries(args.queries)
    print(f"Loaded {len(queries)} queries from {args.queries}")

    # One collection across every query, keyed by tweet id so a tweet that
    # matches several terms is recorded once and keeps all of them.
    completed, records = load_state(args.state)
    if args.restart:
        completed, records = set(), {}
    elif completed:
        print(f"Resuming: {len(completed)} queries already collected, "
              f"{len(records)} tweets already stored")

    for query in queries:
        if query in completed:
            print(f"Skipping '{query}' (already collected)")
            continue

        try:
            tweets, users = fetch_query(query, args.tweets_per_query)

            if not tweets:
                print(f"No tweets returned for query '{query}'")
            else:
                new_ids = 0
                for tweet in tweets:
                    key = str(tweet.id)
                    if key not in records:
                        records[key] = build_record(tweet, users.get(str(tweet.author_id)))
                        new_ids += 1
                    matched = records[key]['matched_queries']
                    if query not in matched:
                        matched.append(query)

                print(f"{query}: {len(tweets)} tweets, {new_ids} new")

        except Exception as e:
            # Each query is independent. A failure here must not stop the
            # remaining queries, otherwise one bad term discards the whole run.
            print(f"Failed to fetch tweets for query '{query}': {type(e).__name__}: {e}")
            continue

        # Only a query that finished without raising is recorded as done, so a
        # failure is retried on the next run instead of being skipped forever.
        completed.add(query)
        save_state(args.state, completed, records)

    if not records:
        print("No tweets collected, nothing written.")
        return

    written = write_export(records, args.output)
    print(f"{written} unique tweets saved to {args.output}")


if __name__ == "__main__":
    main()
