import tweepy
import os
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


# Read queries from the text file next to this script, so the tool works
# regardless of the directory it is launched from.
def load_queries(path=None):
    query_file = Path(path) if path else BASE_DIR / 'queries.txt'
    with open(query_file, encoding='utf-8') as file:
        return [line.strip() for line in file if line.strip()]

# Main Function
def main():
    queries = load_queries()
    count = int(input("Enter the number of queries: "))
    for query in queries:
        try:
            # Search for tweets for the current query
            tweets, users = fetch_query(query, count)

            if not tweets:
                print(f"No tweets returned for query '{query}'")
                continue

            tweets_data = []

            for tweet in tweets:
                user = users.get(str(tweet.author_id))
                followers = None
                if user is not None:
                    followers = (getattr(user, 'public_metrics', None) or {}).get('followers_count')

                # The raw entities object is a nested dict, which pandas writes as
                # an unparseable Python repr. Keep only the hashtags as a
                # pipe-separated list so the column is usable for analysis.
                entities = tweet.entities or {}
                hashtags = '|'.join(
                    tag for tag in (h.get('tag', '') for h in (entities.get('hashtags') or [])) if tag
                )

                tweets_data.append({
                    'id': tweet.id,
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
                    'hashtags': hashtags
                })

            # Remove duplicates by data frame
            df = pd.DataFrame(tweets_data)
            df.drop_duplicates(subset=['text'], inplace=True)
    
            safe_query = query.replace("#", "").replace(" ", "_")

            # Save the DataFrame to a CSV file
            df.to_csv(f'tweets_{safe_query}.csv', index=False)
            print(f"Tweets saved to tweets_{safe_query}.csv")

        except Exception as e:
            # Each query is independent. A failure here must not stop the
            # remaining queries, otherwise one bad term discards the whole run.
            print(f"Failed to fetch tweets for query '{query}': {type(e).__name__}: {e}")

    print("All queries processed.")

if __name__ == "__main__":
    main()
