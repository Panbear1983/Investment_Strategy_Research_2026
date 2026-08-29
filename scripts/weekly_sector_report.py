#!/usr/bin/env python3
"""Weekly sector report from ISR 2026 DB — with LLM synthesis like Israeli semi report."""
import argparse
import json
import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import urllib.request

SCRIPTS_DIR = Path(__file__).resolve().parent
WORKFLOW_PATH = SCRIPTS_DIR / 'research_workflow.sqlite3'
TG_CONFIG_PATH = SCRIPTS_DIR / 'tg_config.json'
ARCHIVE_DIR = SCRIPTS_DIR.parent / 'weekly_db_update'

def _load_config():
    config = json.loads(TG_CONFIG_PATH.read_text(encoding='utf-8'))
    token = config.get('bot_token')
    recipients = config.get('weekly_report_recipients', [])
    if not token:
        raise RuntimeError('bot_token missing in tg_config.json')
    if not recipients:
        raise RuntimeError('weekly_report_recipients missing in tg_config.json')
    return token, recipients

def _send_telegram(token, chat_id, text):
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    data = json.dumps({'chat_id': chat_id, 'text': text, 'disable_web_page_preview': True}).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def archive_weekly_report(report, sector, period_label, *, pushed_at=None, archive_dir=ARCHIVE_DIR):
    """Append one delivered weekly report to its Taiwan push-year Markdown archive.

    The marker makes retries idempotent: a Telegram delivery retry cannot add a
    second copy of the same sector/week report.
    """
    if not report.strip():
        raise ValueError('cannot archive an empty weekly report')
    pushed_at = pushed_at or datetime.now(ZoneInfo('Asia/Taipei'))
    if pushed_at.tzinfo is None:
        raise ValueError('pushed_at must be timezone-aware')
    local = pushed_at.astimezone(ZoneInfo('Asia/Taipei'))
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f'{local.year}.md'
    marker = f'<!-- isr-weekly-report sector={sector} period={period_label} -->'
    if target.exists() and marker in target.read_text(encoding='utf-8'):
        return {'status': 'already_archived', 'path': str(target), 'marker': marker}

    heading = f'## 推送日期：{local.date().isoformat()}｜{sector}｜資料區間：{period_label}'
    entry = f'\n{marker}\n\n{heading}\n\n{report.rstrip()}\n'
    existing = target.read_text(encoding='utf-8') if target.exists() else (
        f'# ISR 2026 每週資料庫更新報告｜{local.year}\n'
    )
    fd, temporary = tempfile.mkstemp(prefix=f'.{target.name}.', dir=archive_dir, text=True)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(existing.rstrip() + entry)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return {'status': 'archived', 'path': str(target), 'marker': marker}


def _rolling_window(now=None):
    """Return the exact preceding seven-day Taiwan-time window ending at `now`."""
    end = now or datetime.now(ZoneInfo('Asia/Taipei'))
    if end.tzinfo is None:
        raise ValueError('report window end must be timezone-aware')
    end = end.astimezone(ZoneInfo('Asia/Taipei'))
    start = end - timedelta(days=7)
    label = f'{start:%Y-%m-%d %H:%M} 至 {end:%Y-%m-%d %H:%M}（台灣時間）'
    return start, end, label


def _utc_bounds(window_start, window_end):
    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise ValueError('report window must be timezone-aware')
    if window_start >= window_end:
        raise ValueError('report window start must precede end')
    return (
        window_start.astimezone(ZoneInfo('UTC')).isoformat(timespec='seconds'),
        window_end.astimezone(ZoneInfo('UTC')).isoformat(timespec='seconds'),
    )

def _fetch_raw_posts(sector, window_start, window_end, limit=20):
    utc_start, utc_end = _utc_bounds(window_start, window_end)
    # Support both English and Chinese sector keywords
    sector_lower = sector.lower()
    # If user provides English, also try Chinese mapping
    sector_map = {
        'semiconductor': '半導體',
        'biotech': '生技',
        'ai': '人工智慧',
        'defense': '國防',
        'cloud': '雲端',
    }
    chinese_sector = sector_map.get(sector_lower, sector_lower)
    # Use OR to match either language
    sector_pattern = f'%{chinese_sector}%' if chinese_sector != sector_lower else f'%{sector_lower}%'
    if chinese_sector != sector_lower:
        sector_pattern = f'%{sector_lower}%' + ' OR ' + f'%{chinese_sector}%'
    
    with sqlite3.connect(WORKFLOW_PATH) as db:
        db.row_factory = sqlite3.Row
        if chinese_sector != sector_lower:
            # Two patterns
            rows = db.execute("""
                SELECT cr.ticker, cr.company_name, cr.record_json, cr.updated_at, cr.source
                FROM company_research cr
                JOIN companies c ON c.id = (
                    SELECT id FROM companies WHERE ticker = cr.ticker LIMIT 1
                )
                WHERE cr.updated_at >= ? AND cr.updated_at < ?
                  AND (json_extract(cr.record_json, '$.Industry') LIKE ? OR json_extract(cr.record_json, '$.Industry') LIKE ?)
                ORDER BY cr.updated_at DESC
                LIMIT ?
            """, (utc_start, utc_end, f'%{sector_lower}%', f'%{chinese_sector}%', limit)).fetchall()
        else:
            rows = db.execute("""
                SELECT cr.ticker, cr.company_name, cr.record_json, cr.updated_at, cr.source
                FROM company_research cr
                JOIN companies c ON c.id = (
                    SELECT id FROM companies WHERE ticker = cr.ticker LIMIT 1
                )
                WHERE cr.updated_at >= ? AND cr.updated_at < ?
                  AND json_extract(cr.record_json, '$.Industry') LIKE ?
                ORDER BY cr.updated_at DESC
                LIMIT ?
            """, (utc_start, utc_end, f'%{sector_lower}%', limit)).fetchall()
    
    return rows

def _build_llm_prompt(sector, period_label, rows):
    companies_data = []
    for row in rows:
        rec = json.loads(row['record_json'])
        companies_data.append({
            'company_name': row['company_name'],
            'ticker': row['ticker'],
            'industry': rec.get('Industry', ''),
            'market_cap': rec.get('Capital/Market Cap', ''),
            'core_business': rec.get('Core Business', ''),
            'revenue_breakdown': rec.get('Revenue Breakdown', ''),
            'gross_margin': rec.get('Gross Margin Profile', ''),
            'catalysts': rec.get('12M Catalysts', ''),
            'risks': rec.get('Key Investment Risks', ''),
            'updated_at': row['updated_at'][:10],
            'source': row['source']
        })
    
    prompt = f"""你是專業產業分析師。根據以下 {sector} 產業資料區間（{period_label}）的 {len(companies_data)} 家公司原始 22 欄位資料，生成一份結構化的中文週報。

要求：
1. **分類分組**：將公司依業務性質分為「晶圓製造端」、「製程檢測／量測設備端」、「Fabless／IP設計端」等段落
2. **每家公司條列**：公司名(代碼)｜定位(龍頭/隱形冠軍/潛力股)｜市值 → 核心業務、關鍵數據、催化劑、風險
3. **共同風險主題**：最後補充一段「共同風險主題」，歸納跨公司的系統性風險（地緣、出口管制、估值等）
4. **格式**：完全參考範例格式（標題、分段、條列、粗體標記）
5. **只用提供資料**：不得補充外部知識，所有數字必須來自資料

公司資料：
{json.dumps(companies_data, ensure_ascii=False, indent=2)}

請直接輸出完整中文報告，不需額外說明。"""
    return prompt

def _call_llm(prompt):
    """Call Claude CLI (per preferences.yaml chat ladder: claude_cli first)."""
    try:
        result = subprocess.run(
            ['claude', '-p', prompt, '--model', 'sonnet'],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            raise RuntimeError(f'claude exit {result.returncode}: {result.stderr}')
        return result.stdout.strip()
    except FileNotFoundError:
        # Fallback to codex
        result = subprocess.run(
            ['codex', 'exec', prompt],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            raise RuntimeError(f'codex exit {result.returncode}: {result.stderr}')
        return result.stdout.strip()

def build_synthesized_report(sector, window_start, window_end, limit=20):
    period_label = _rolling_window(window_end)[2] if window_start == window_end - timedelta(days=7) else (
        f'{window_start:%Y-%m-%d %H:%M} 至 {window_end:%Y-%m-%d %H:%M}（台灣時間）'
    )
    rows = _fetch_raw_posts(sector, window_start, window_end, limit)
    if not rows:
        return f"📊 {sector} 週報｜{period_label}\n\n本週無更新資料。"

    prompt = _build_llm_prompt(sector, period_label, rows)
    report = _call_llm(prompt)
    header = f"📊 {sector} 週報｜{period_label}\n共 {len(rows)} 筆更新\n\n"
    return header + report


def build_raw_report(sector, window_start, window_end, limit=20):
    """Deterministic fallback using the same rolling database-update window."""
    period_label = f'{window_start:%Y-%m-%d %H:%M} 至 {window_end:%Y-%m-%d %H:%M}（台灣時間）'
    rows = _fetch_raw_posts(sector, window_start, window_end, limit)
    if not rows:
        return f"📊 {sector} 週報｜{period_label}\n\n本週無更新資料。"

    lines = [f"📊 {sector} 週報｜{period_label}", f"共 {len(rows)} 筆更新：", ""]
    for row in rows:
        rec = json.loads(row['record_json'])
        lines.extend([
            f"🏢 {row['company_name']} ({row['ticker']})",
            f"   產業：{rec.get('Industry', 'N/A')}",
            f"   市值：{rec.get('Capital/Market Cap', 'N/A')}",
            f"   核心業務：{rec.get('Core Business', 'N/A')[:120]}...",
            f"   營收細分：{rec.get('Revenue Breakdown', 'N/A')[:100]}...",
            f"   毛利概況：{rec.get('Gross Margin Profile', 'N/A')[:100]}...",
            f"   關鍵催化：{rec.get('12M Catalysts', 'N/A')[:100]}...",
            f"   主要風險：{rec.get('Key Investment Risks', 'N/A')[:100]}...",
            f"   更新：{row['updated_at'][:10]} | 來源：{row['source']}",
            ""
        ])
    return "\n".join(lines)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sector', required=True, help='Sector keyword, e.g. Semiconductor')
    parser.add_argument(
        '--window-end',
        help='ISO-8601 end time for a deterministic seven-day test window; default is now in Taiwan time',
    )
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--send', action='store_true', help='Actually send to Telegram')
    parser.add_argument('--no-llm', action='store_true', help='Use raw dump instead of LLM synthesis')
    args = parser.parse_args()
    
    now = datetime.fromisoformat(args.window_end) if args.window_end else None
    window_start, window_end, period_label = _rolling_window(now)

    if args.no_llm:
        report = build_raw_report(args.sector, window_start, window_end, args.limit)
    else:
        report = build_synthesized_report(args.sector, window_start, window_end, args.limit)
    
    print(report)
    
    if args.send:
        token, recipients = _load_config()
        for r in recipients:
            result = _send_telegram(token, r['chat_id'], report)
            if not result.get('ok'):
                raise RuntimeError(f"Telegram delivery failed for {r['label']}: {result}")
            print(f"Telegram → {r['label']} ({r['chat_id']}): sent ✓")
        archive = archive_weekly_report(report, args.sector, period_label)
        print(f"Archive → {archive['status']}: {archive['path']}")

if __name__ == '__main__':
    main()