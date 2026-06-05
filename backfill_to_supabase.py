import os
import csv
from dotenv import load_dotenv
from supabase import create_client

def backfill():
    load_dotenv('.env')
    url = os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key or "your_" in url:
        print("Please update your .env file with your actual Supabase URL and Key before running this.")
        return

    supabase = create_client(url, key)
    
    csv_file = 'apex_matches.csv'
    if not os.path.exists(csv_file):
        print(f"No {csv_file} found in the current directory.")
        return

    print("Reading local CSV...")
    rows_to_insert = []
    with open(csv_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Clean up empty strings to None if necessary, but dict works directly 
            # if the schema types match (strings are cast to int automatically by Postgres if valid)
            rows_to_insert.append({
                "timestamp": row["timestamp"],
                "session_id": row["session_id"],
                "squad_placed": int(row["squad_placed"]) if row["squad_placed"] else None,
                "total_squad_kills": int(row["total_squad_kills"]) if row["total_squad_kills"] else None,
                "player_slot": int(row["player_slot"]) if row["player_slot"] else None,
                "name": row["name"],
                "kills": int(row["kills"]) if row["kills"] else 0,
                "assists": int(row["assists"]) if row["assists"] else 0,
                "knocks": int(row["knocks"]) if row["knocks"] else 0,
                "damage": int(row["damage"]) if row["damage"] else 0,
                "revive_given": int(row["revive_given"]) if row["revive_given"] else 0,
                "respawn_given": int(row["respawn_given"]) if row["respawn_given"] else 0,
            })

    if not rows_to_insert:
        print("CSV is empty.")
        return

    print(f"Found {len(rows_to_insert)} records. Pushing to Supabase...")
    try:
        # Supabase limits inserts to ~1000 rows at a time, batch it if it's huge
        batch_size = 500
        for i in range(0, len(rows_to_insert), batch_size):
            batch = rows_to_insert[i:i+batch_size]
            supabase.table("apex_matches").insert(batch).execute()
            print(f"Inserted batch {i//batch_size + 1}...")
            
        print("Backfill complete! You can now start your Next.js dashboard.")
    except Exception as e:
        print(f"Error during backfill: {e}")

if __name__ == "__main__":
    backfill()
