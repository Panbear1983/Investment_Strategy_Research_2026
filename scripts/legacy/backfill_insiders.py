import csv
import json
import os
import re
import urllib.request
import ssl
import time

context = ssl._create_unverified_context()

def get_insider_data(ticker):
    # Ensure it's a clean uppercase ticker (US style, e.g. GOOGL, MSFT)
    # If it's a number (Taiwan style, e.g. 2330), return No Data
    if not ticker or ticker.isdigit():
        return "無資料 (美股限定)"
    
    url = f"https://www.quiverquant.com/insiders/{ticker}"
    req = urllib.request.Request(
        url, 
        headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
    )
    try:
        with urllib.request.urlopen(req, context=context, timeout=5) as response:
            html = response.read().decode('utf-8')
            match = re.search(r'const insiderGraphData = (\[.*?\]);', html)
            if match:
                data_str = match.group(1).replace("'", '"')
                data = json.loads(data_str)
                res = []
                for q in data:
                    quarter = q.get('Quarter', '')
                    sentiment = q.get('Sentiment', 0)
                    if sentiment == 0:
                        val = "$0"
                    elif abs(sentiment) >= 1000000:
                        val = f"{'-' if sentiment < 0 else ''}${abs(sentiment)/1000000:.2f}M"
                    else:
                        val = f"{'-' if sentiment < 0 else ''}${abs(sentiment)/1000:.0f}K"
                    res.append(f"{quarter}: {val}")
                return ", ".join(res)
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
    return "無資料 (美股限定)"

repo_dir = '/Users/peter/GitHub/Investment_Strategy_Research_2026'
csv_path = os.path.join(repo_dir, 'datasets', 'Global_100k_Investment_Database.csv')
temp_csv_path = csv_path + '.tmp'

# Verify columns and backfill
try:
    with open(csv_path, 'r', encoding='utf-8-sig') as infile:
        reader = csv.reader(infile)
        headers = next(reader)
        
        # Check if column already exists
        if "近期內部人交易 (Recent Insider Trading)" not in headers:
            headers.append("近期內部人交易 (Recent Insider Trading)")
            has_col = False
        else:
            has_col = True
            
        rows = list(reader)

    print(f"Starting backfill for {len(rows)} rows...")
    
    with open(temp_csv_path, 'w', newline='', encoding='utf-8-sig') as outfile:
        writer = csv.writer(outfile)
        writer.writerow(headers)
        
        completed = 0
        for i, row in enumerate(rows):
            # Extract ticker from Company name column (index 5) e.g., "Apple (AAPL)"
            company_col = row[5]
            
            # If it's a placeholder row
            if 'Global_Entity' in company_col:
                if not has_col:
                    row.append("等待系統進行深度調查...")
                else:
                    row[21] = "等待系統進行深度調查..."
                writer.writerow(row)
                continue
                
            ticker_match = re.search(r'\((.*?)\)', company_col)
            if ticker_match:
                ticker = ticker_match.group(1).split('.')[0] # Remove .TW or other extensions if present
            else:
                ticker = ""
                
            # For already researched companies, fetch their insider data
            if row[15] != "等待系統進行深度調查..." and row[15] != "調查中":
                insider_summary = get_insider_data(ticker)
                completed += 1
                print(f"[{completed}] Fetched {ticker}: {insider_summary}")
                time.sleep(0.3) # Respectful delay
            else:
                insider_summary = "等待系統進行深度調查..."
                
            if not has_col:
                row.append(insider_summary)
            else:
                row[21] = insider_summary
                
            writer.writerow(row)
            
    os.replace(temp_csv_path, csv_path)
    print("Backfill completed successfully!")
    
except Exception as e:
    print(f"Main Error: {e}")
