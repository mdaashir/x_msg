import tweepy
import os
from dotenv import load_dotenv
import pandas as pd
from time import sleep
from requests.exceptions import ConnectionError, ConnectTimeout

# Load environment variables from .env file
load_dotenv()

bearer_token = os.getenv('BEARER_TOKEN')

# Authenticate using the Bearer Token
client = tweepy.Client(bearer_token=bearer_token, wait_on_rate_limit=True)

# Function to fetch tweets with retries
def fetch_tweets(keyword, max_count=100, retries=3):
    for i in range(retries):
        try:
            tweet_data = client.search_recent_tweets(query=keyword, max_results=max_count, tweet_fields=['id', 'text', 'author_id', 'created_at', 'lang', 'public_metrics', 'entities'])
            return tweet_data
        except (ConnectionError, ConnectTimeout) as connection_error:
            print(f"Connection error: {connection_error}. Retrying ({i+1}/{retries})...")
            sleep(5)
    raise Exception("Failed to fetch tweets after several retries")

# Read queries from the text file
with open('queries.txt', 'r') as file:
    queries = file.read().splitlines()

# Main Function
def main():
    count = int(input("Enter the number of queries: "))
    for query in queries:
        # Skip empty lines
        if query.strip() == "": continue
    
        try:
            # Search for tweets for the current query
            tweets = fetch_tweets(query, count)

            tweets_data = []

            for tweet in tweets.data:
                try:
                    user = client.get_user(id=tweet.author_id)
                    username = user.data.username
                except tweepy.TweepyException as username_error:
                    print(f"Failed to get user info: {username_error}")
                    username = "Unknown"

                tweets_data.append({
                    'id': tweet.id,
                    'username': username,
                    'text': tweet.text,
                    'created_at': tweet.created_at,
                    'lang': tweet.lang,
                    'retweet_count': tweet.public_metrics['retweet_count'],
                    'reply_count': tweet.public_metrics['reply_count'],
                    'like_count': tweet.public_metrics['like_count'],
                    'quote_count': tweet.public_metrics['quote_count'],
                    'entities': tweet.entities
                })

            # Remove duplicates by data frame
            df = pd.DataFrame(tweets_data)
            df.drop_duplicates(subset=['text'], inplace=True)
    
            safe_query = query.replace("#", "").replace(" ", "_")

            # Save the DataFrame to a CSV file
            df.to_csv(f'tweets_{safe_query}.csv', index=False)
            print(f"Tweets saved to tweets_{safe_query}.csv")

        except tweepy.TweepyException as e:
            print(f"Failed to fetch tweets for query '{query}': {e}")

    print("All queries processed.")

if __name__ == "__main__":
    main()