import csv
import os

repo_dir = '/Users/peter/GitHub/Investment_Strategy_Research_2026'
csv_path = os.path.join(repo_dir, 'datasets', 'Global_100k_Investment_Database.csv')
temp_csv_path = csv_path + '.tmp'

# Priority order
country_priority = {
    'Taiwan': 1,
    'USA': 2,
    'Japan': 3,
    'South Korea': 4,
    'China': 5,
    'Israel': 6,
    'France': 7,
    'Germany': 8,
    'Netherlands': 9,
    'UK': 10
}

def get_country_priority(country):
    # Match standard country name or default to 99
    country = country.strip()
    return country_priority.get(country, 99)

def get_row_category(row):
    company = row[5]
    status = row[7] # Core Business
    
    is_placeholder = 'Global_Entity' in company or 'Placeholder' in company.lower() or company.startswith('全球實體')
    is_pending = '等待' in status or status == '' or status == '—' or '等待' in row[9] # Technical Moat
    
    if is_placeholder:
        return 2 # Placeholders
    elif is_pending:
        return 1 # Pending actuals
    else:
        return 0 # Completed actuals

try:
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        headers = next(reader)
        rows = list(reader)
        
    # Sort key: (Category, Country Priority, index) to ensure stable sort
    sorted_rows = [
        r for idx, r in sorted(
            enumerate(rows),
            key=lambda item: (
                get_row_category(item[1]),
                get_country_priority(item[1][0]),
                item[0]
            )
        )
    ]
    
    with open(temp_csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(sorted_rows)
        
    os.replace(temp_csv_path, csv_path)
    print("Re-sorted database successfully according to completion categories and country priority order.")

except Exception as e:
    print(f"Error during sorting: {e}")
