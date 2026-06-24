# Apex Legends Status (ALS) RP Tracking Spec

## Objective
Add a background polling mechanism to the local Python tracker that queries the unofficial Apex Legends Status (ALS) API to track Rank and RP changes for the squad. The fetched RP data must be accurately tied to the existing OCR match logs (kills, damage, placement) and saved to both `apex_matches.csv` and Supabase.

## Core Constraints & API Limitations
1. **API Rate Limit:** The free ALS API limits you to **5 queries per second** across all APIs. While faster than typical free APIs, the script should enforce a minimum 1-second sleep between individual player requests (or check the `X-Current-Rate` header) to safely stay under the limit.
2. **"Open Profile" Mimicry:** The background thread must poll the API every 120 seconds to mimic an active browser session. This ensures the ALS backend considers the profiles "active" and updates the data.
3. **The EA Cache Delay (Critical):** 
   - EA's public servers (which ALS reads from) have a 2-3 minute cache delay. 
   - When the OCR captures the end-of-match screen, the EA servers **will not** have the updated `ending_rp` yet. 
   - The code must account for this delay rather than querying the API instantly and getting stale data.

## Proposed Architecture: "Smart Polling"

### 1. `config.json` & Environment Variables
- **`.env`:** Add a new variable `ALS_API_KEY`.
- **`config.json`:** Add a `rank_poll_seconds` setting (default `120`). Assume `PC` as the default platform, but write the code so platform could theoretically be configured per-player in the future.

### 2. `rank_tracker.py` (New Module)
Create a thread-safe `RankTracker` class using standard Python `threading` and `urllib.request` (to keep overhead low).
- **Initialization:** Accepts the API key, the `known_names` list from the config, and the poll interval.
- **Background Loop:** A daemon thread that loops indefinitely. It iterates through the player list, fetching data from:
  `https://api.apexlegendsstatus.com/bridge?player={name}&platform=PC`
  *Pass the API key using the `Authorization: YOUR_API_KEY` HTTP header.*
- **Cache Logic:** Maintains a thread-safe dictionary holding each player's `current_rp` and `previous_rp`.
- **API Response Parsing:** The RP is found in the JSON response under `global.rank.rankScore`.
- **Error Handling:** Must handle standard API errors gracefully without crashing the thread:
  - `400`: Try again later (EA API issue)
  - `403`: Unauthorized / Bad API Key
  - `404`: Player not found
  - `429`: Rate limit reached
- **Rate Limiting:** `time.sleep(1.0)` between each player request.

### 3. `apex_tracker.py` (Modifications)
- **Initialization:** Instantiate the `RankTracker` in the global scope or command setup (similar to the Supabase client).
- **Schema Update:** Update `CSV_FIELDS` to include `starting_rp`, `ending_rp`, and `rp_change`.
- **Match Logging Integration:** 
  - Because of the EA cache delay, we cannot instantly write the `ending_rp` when `extract_match` runs. 
  - **Implementation Choice:** The agent should implement a system where, after a match is detected by OCR, the tracker flags those players and waits for their cached RP to actively change in the background *before* committing the final `ending_rp` and `rp_change` to the CSV and Supabase.
  - Alternatively, if waiting is too complex, implement an "Accelerated Polling" mode where the script checks the API 60 seconds and 120 seconds after the match ends to grab the final RP before writing the database row.
