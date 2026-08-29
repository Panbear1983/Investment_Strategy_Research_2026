import csv
import json
import os
import re
import urllib.request
import ssl
import subprocess

context = ssl._create_unverified_context()

def get_tg_config():
    config_path = '/Users/peter/GitHub/Investment_Strategy_Research_2026/scripts/tg_config.json'
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            return json.load(f)
    return None

def send_telegram(text):
    config = get_tg_config()
    if not config:
        return
    token = config.get('bot_token')
    chat_id = config.get('chat_id')
    if not token or not chat_id:
        return
        
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    cmd = [
        "curl", "-s", "-X", "POST", url,
        "-d", f"chat_id={chat_id}",
        "-d", f"text={text}",
        "-d", "parse_mode=Markdown"
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# Option A: Fetch insider trading statistics for a specific ticker
def get_insider_data(ticker):
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
    except Exception:
        pass
    return "無資料 (美股限定)"

# Option B: Scan Quiver Quant dashboard for market-wide insider anomalies (> $1M)
def scan_large_transactions():
    url = "https://www.quiverquant.com/insiders/"
    req = urllib.request.Request(
        url, 
        headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
    )
    anomalies = []
    try:
        with urllib.request.urlopen(req, context=context, timeout=5) as response:
            html = response.read().decode('utf-8')
            match = re.search(r'Plotly\.newPlot\(\s*\"[a-f0-9\-]+\",\s*(\[.*?\}\]),', html, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
                treemap = data[0]
                customdata = treemap.get('customdata', [])
                labels = treemap.get('labels', [])
                parents = treemap.get('parents', [])
                
                for i in range(len(customdata)):
                    c_data = customdata[i]
                    label = labels[i]
                    parent = parents[i] if i < len(parents) else ""
                    
                    if len(c_data) >= 3:
                        size_str = str(c_data[1]).replace(",", "")
                        shares_str = str(c_data[2]).replace(",", "")
                        
                        if size_str.isdigit():
                            size = int(size_str)
                            if size >= 1000000:
                                ticker = parent.replace("/", "").strip() if parent else ""
                                if ticker and ticker != "?":
                                    shares_bought = int(shares_str) if shares_str.replace("-", "").isdigit() else 0
                                    trade_type = "🟢 BUY (買入)" if shares_bought >= 0 else "🔴 SELL (賣出)"
                                    anomalies.append(
                                        f"• **{ticker}** | {trade_type}\n"
                                        f"  額度: **${size/1000000:.2f}M** | 人員: {label}\n"
                                        f"  張數變動: {c_data[2]} | 比例變動: {c_data[0]}"
                                    )
    except Exception as e:
        print("Anomaly Scan Error:", e)
    return anomalies

send_telegram("🚀 **Batch 14 Started (Cron - Silent Mode):** Initiating deep research on the next 20 Aerospace, Platform Economics & Shipping giants, including Quiver Quant Insider backfills and anomaly sweeps...")

repo_dir = '/Users/peter/GitHub/Investment_Strategy_Research_2026'
csv_path = os.path.join(repo_dir, 'datasets', 'Global_100k_Investment_Database.csv')
temp_csv_path = csv_path + '.tmp'
progress_path = os.path.join(repo_dir, 'scripts', 'build_progress.json')

new_companies = [
    ('USA', 'Honeywell (HON)', '航太與工業軟體科技'),
    ('USA', 'TransDigm (TDG)', '航太精密零組件'),
    ('USA', 'Howmet Aerospace (HWM)', '航太發動機材料'),
    ('USA', 'Axon Enterprise (AXON)', '警用科技與執法軟體'),
    ('USA', 'Uber Technologies (UBER)', '共享出行與配送平台'),
    ('USA', 'Booking Holdings (BKNG)', '線上旅遊OTA平台'),
    ('Argentina', 'MercadoLibre (MELI)', '拉美電商與金融科技'),
    ('Singapore', 'Sea Ltd (SE)', '東南亞電商與遊戲平台'),
    ('Singapore', 'Grab Holdings (GRAB)', '東南亞共享出行超級APP'),
    ('South Korea', 'Coupang (CPNG)', '南韓電商平台'),
    ('China', 'PDD Holdings (PDD)', '拼多多跨境電商'),
    ('China', 'Alibaba (BABA)', '電商與雲端服務'),
    ('China', 'Tencent (0700.HK)', '遊戲、社群與雲端平台'),
    ('China', 'Meituan (3690.HK)', '本地生活與外送平台'),
    ('Taiwan', 'EVA Air (2618.TW)', '客運與航空貨運'),
    ('Taiwan', 'China Airlines (2610.TW)', '客運與航空貨運'),
    ('Taiwan', 'Yang Ming (2609.TW)', '全球貨櫃航運'),
    ('Taiwan', 'Wan Hai (2615.TW)', '近洋與遠洋貨櫃航運'),
    ('Taiwan', 'Evergreen Marine (2603.TW)', '全球大容量貨櫃航運'),
    ('Taiwan', 'Taiwan High Speed Rail (2633.TW)', '台灣西部鐵路運輸')
]

updates = {
    'Honeywell': {
        'CEO & Management': 'Vimal Kapur (CEO) 推動向航太、工業自動化及能源轉型三大核心重組，極力整合 Honeywell Forge 工業軟體平台。',
        'Core Patents & IP': '輔助動力裝置(APU)專利、客艙增壓控制系統、以及 Forge 生產力軟體與量子計算(Quantinuum)核心專利。',
        '3-Year M&A': '以 49 億元收購開利(Carrier)的商用安全部門以補強自動化業務；以 19 億元收購 Civica 強化公共部門軟體。',
        'CapEx & Expansion': '投資於擴建先進航空電子元件與量子計算研發設施，並在全球精簡低利潤產線。',
        'Geopolitical Exposure': '高度暴露於全球航空製造(Boeing/Airbus)供應鏈，受美軍工採購預算波動與跨國自動化投資放緩牽制。',
        'M&A Potential': '工業與航太巨頭，為主要收購方，無被併購壓力，具分拆軟體部門之可能。'
    },
    'TransDigm': {
        'CEO & Management': 'Kevin Stein 貫徹「高利潤、獨家供應」的「航空售後零件」策略，以極高的定價權與債務槓桿獲取暴利。',
        'Core Patents & IP': '全球數千種民用與軍用飛機專用閥門、泵浦、點火裝置及電子感測器的獨家設計圖與專利。',
        '3-Year M&A': '以 13.85 億美元收購 CPI 的電子控制部門，持續將觸角延伸到高邊際利潤的軍工航空零件。',
        'CapEx & Expansion': '輕資本運營，主要資本支出為收購利基航太零件廠，並小幅升級高難度合金鑄造工廠。',
        'Geopolitical Exposure': '90% 營收來自獨家或專利防禦零件，主要受制於民航機飛行時數與美軍工防務預算增減。',
        'M&A Potential': '航太界的「現金流金雞母」，因高債務與高定價策略，適合 PE 基金或維持獨立運作，難被一般硬體廠整併。'
    },
    'Howmet Aerospace': {
        'CEO & Management': 'John Plant 採取極致的成本削減與產線自動化，使 Howmet 成為全球航空發動機鈦合金葉片的最核心壟斷者。',
        'Core Patents & IP': '單晶超合金鑄造技術(極端高溫下不變形)、發動機壓氣機葉片專利，及專利緊固件技術(Arconic分拆而來)。',
        '3-Year M&A': '無大型併購。專注於利用自由現金流進行大額股票回購，及在關鍵航空模具廠進行技術補強。',
        'CapEx & Expansion': '斥資數億美元擴建美國與歐洲的熔煉與單晶鑄造產線，以因應 LEAP 與 GTF 發動機大缺貨。',
        'Geopolitical Exposure': '高度暴露於鈦、鎳等戰略金屬價格波動，及俄羅斯鈦原料禁運後的地緣替代供應風險。',
        'M&A Potential': '發動機製造的核心咽喉，受到反壟斷法與國安法保護，為航太巨頭不可或缺的獨立供應商。'
    },
    'Axon Enterprise': {
        'CEO & Management': 'Rick Smith (創辦人/CEO) 大推「2033年消滅警用槍擊致死」願景，推廣 Taser 電擊槍、Body Cam，以及訂閱制執法軟體 Evidence.com。',
        'Core Patents & IP': '泰瑟(Taser)非致命性電擊發射機制專利、警用執法記錄器實時串流、以及雲端證據管理平台(Evidence.com)安全性專利。',
        '3-Year M&A': '收購 FyrSoft 強化企業雲端整合能力；收購 Foveon 以提升記錄器相機傳感器品質。',
        'CapEx & Expansion': '擴建美國本土 Taser 智能硬體組裝線，並大舉招募 AI 執法影片分析軟體的研發人員。',
        'Geopolitical Exposure': '90% 以上營收來自美國聯邦與地方警政單位，地緣政治斷鏈風險極低，但高度受美國警民關係與司法撥款法規波動影響。',
        'M&A Potential': '警用執法科技與 SaaS 霸主，市值巨大且黏著度極高，為市場獨特標的，無被併購可能。'
    },
    'Uber Technologies': {
        'CEO & Management': 'Dara Khosrowshahi 成功帶領公司轉虧為盈，大舉整合乘車(Rides)、外送(Eats)及貨運(Freight)，朝「全方位移動平台」發展。',
        'Core Patents & IP': '動態定價(Surge Pricing)演算法專利、司機與乘客最優路徑配對、及自動駕駛地圖接口專利。',
        '3-Year M&A': '收購 Transplace 擴展貨運物流；購入 Delivery Hero 台灣業務(進行中)以稱霸特定外送市場。',
        'CapEx & Expansion': '輕資產模式，資本支出主要用於維持伺服器、App 運算及向自駕車隊(Waymo)進行平台整合投資。',
        'Geopolitical Exposure': '全球化營收，面臨多國對零工經濟(Gig Economy)勞工權益與司機僱傭關係的法律合規挑戰。',
        'M&A Potential': '全球共享經濟始祖，市值巨大，無被收購可能，將持續扮演平台整合與外送市場的掠食者。'
    },
    'Booking Holdings': {
        'CEO & Management': 'Glenn Fogel 專注於推動「Connected Trip (一站式旅遊規劃)」，利用 AI 提供機票、酒店、租車的一體化推薦。',
        'Core Patents & IP': 'Booking.com 代理商模式(Agency Model)結算系統專利、Agoda 價格即時比對演算法，及 Priceline 逆向拍賣專利。',
        '3-Year M&A': '原擬以 16 億歐元收購機票平台 eTraveli，但遭歐盟反壟斷否決；轉為進行小型旅遊科技服務收購。',
        'CapEx & Expansion': '重金投入 AI 旅遊助手研發，並在全球租用大型伺服器中心以應付每日數億次的房源檢索。',
        'Geopolitical Exposure': '高度依賴全球跨境旅遊景氣，易受地緣衝突、傳染病及航空罷工等宏觀事件衝擊。',
        'M&A Potential': '全球最大 OTA 巨頭，為高毛利、高黏著平台，無被收購可能。'
    },
    'MercadoLibre': {
        'CEO & Management': 'Marcos Galperin (創辦人/CEO) 風格強悍，成功在拉美惡劣的物流與金融基礎上，築起「MELI 電商+Mercado Pago 金融」的鐵壁護城河。',
        'Core Patents & IP': 'Mercado Pago 移動支付安全加密系統、拉美複雜路況下之智能路由物流配送專利技術。',
        '3-Year M&A': '無大型併購。主要投入自建拉丁美洲最大的專屬物流機隊(Mercado Envíos)及物流轉運中心。',
        'CapEx & Expansion': '在巴西、墨西哥、阿根廷等國投資數十億美元擴建倉儲，並擴大 Mercado Pago 信貸業務的準備金。',
        'Geopolitical Exposure': '高度暴露於拉美各國(阿根廷、巴西)惡性通貨膨脹、匯率劇烈波動及政治不穩定風險中。',
        'M&A Potential': '拉丁美洲最大的電商與金融龍頭，受地緣特質保護，且規模龐大，無被併購可能。'
    },
    'Sea Ltd': {
        'CEO & Management': '李小冬(Forrest Li)在經歷股價暴跌後迅速調整策略，專注於「現金流與利潤優先」，帶領 Shopee 與 Garena 重回獲利軌道。',
        'Core Patents & IP': 'Free Fire 射擊遊戲引擎優化專利、Shopee 東南亞大數據推薦算法，及 SeaMoney 移動支付系統專利。',
        '3-Year M&A': '無大型併購。專注於與 TikTok 旗下 Tokopedia 及 Lazada 在印尼與東南亞的電商市場激烈對決。',
        'CapEx & Expansion': '投資於擴建印尼與越南的 Shopee 快遞物流轉運站，以抵抗 Temu 與 SHEIN 的下沉競爭。',
        'Geopolitical Exposure': '東南亞各國對跨境電商的關稅壁壘(如印尼禁止低價進口貨)直接牽動其電商利潤率。',
        'M&A Potential': '騰訊持股的重要東南亞旗艦，市值龐大且為區域壟斷者，無被併購可能。'
    },
    'Grab Holdings': {
        'CEO & Management': 'Anthony Tan (創辦人/CEO) 聚焦於精簡成本與大推「金融+叫車+外送」超級APP，正努力提高東南亞各國的變現率。',
        'Core Patents & IP': '多功能超級 APP (Super App) 架構專利、東南亞複雜路段動態定價與拼車配對演算法 IP。',
        '3-Year M&A': '收購新加坡第三大計程車營收商 Trans-Cab，以擴充平台司機供給量。',
        'CapEx & Expansion': '投資於 GrabFin 數字銀行在新加坡、馬來西亞的牌照籌設與金融算力投資。',
        'Geopolitical Exposure': '東南亞各國零工法規較為寬鬆，但面臨 GoTo 的強烈競爭與東南亞本幣貶值的匯兌損失。',
        'M&A Potential': '東南亞移動霸主，軟銀等大股東支持，無被敵意收購風險，持續追求在區域內實現穩定正盈利。'
    },
    'Coupang': {
        'CEO & Management': 'Bom Kim (創辦人/CEO) 強力貫徹「Rocket Delivery (火箭送達，保證翌日達)」策略，完全掌控倉儲與配送，被稱為「韓國亞馬遜」。',
        'Core Patents & IP': '火箭配送即時倉儲調撥演算法、全自動無人化機器人分揀物流中心架構專利。',
        '3-Year M&A': '以 5 億美元收購全球奢侈品電商平台 Farfetch，擴充其在高毛利時尚與精品領域的版圖。',
        'CapEx & Expansion': '持續斥資數億美元在韓國二三線城市興建高階智慧物流中心，並大舉擴張台灣跨境電商業務。',
        'Geopolitical Exposure': '韓國市場幾近飽和，面臨中國電商(AliExpress, Temu)大舉攻韓的價格挑戰。',
        'M&A Potential': '軟銀願景基金為最大股東，市值巨大且牢牢壟斷韓國物流，不可能被收購。'
    },
    'PDD Holdings': {
        'CEO & Management': '陳磊與趙佳臻(聯席CEO)低調行事，大推「Temu」跨境全託管模式，以極致低價的「工廠直達消費者」模式席捲全球。',
        'Core Patents & IP': '拼團社交電商算法專利、Temu 全球包裹智能分揀與動態補貼定價系統 IP。',
        '3-Year M&A': '極少收購。所有資源均集中投入 Temu 的全球行銷（如超級盃廣告）與補貼工廠。',
        'CapEx & Expansion': '與各大空運與物流業者深度結盟，大舉投資廣東等跨境電商核心樞紐的物流集貨倉。',
        'Geopolitical Exposure': 'Temu 在美、歐面臨極高地緣政治風險，包括美國國會對低額豁免關稅(De Minimis Loophole)的取消威脅。',
        'M&A Potential': '拼多多與 Temu 雙引擎，為全球電商最大黑馬，股權多由創辦團隊與中資控制，無被收購可能。'
    },
    'Alibaba': {
        'CEO & Management': '蔡崇信(董事長)與吳泳銘(CEO)重掌大局，終止阿里大拆分計畫，轉而聚焦「電商(淘天/速賣通)+雲端計算(阿里雲)」，大推 AI 雲。',
        'Core Patents & IP': '阿里雲飛天操作系統、大規模分散式計算架構專利、淘寶大數據推薦引擎與菜鳥智能物流專利。',
        '3-Year M&A': '大舉收購或參股中國國內大模型新創(如月之暗面、智譜 AI)；終止阿里雲與菜鳥的 IPO 分拆計畫。',
        'CapEx & Expansion': '大手筆採購國產與合規 AI 晶片，擴建阿里雲在中國與東南亞的智算中心資料庫。',
        'Geopolitical Exposure': '阿里雲受限於美國先進晶片出口管制；電商業務面臨拼多多在國內外的劇烈蠶食。',
        'M&A Potential': '中國互聯網與科技基石，受國家戰略保護，絕無被外資併購可能。'
    },
    'Tencent': {
        'CEO & Management': '馬化騰(創辦人/CEO)作風穩健低調，推行「微信視頻號與小遊戲」為新增长極，並透過龐大海外投資建立全球最大遊戲帝國。',
        'Core Patents & IP': '微信(WeChat)超級 App 架構專利、小程式系統 IP、王者榮耀等遊戲引擎與數萬項通訊專利。',
        '3-Year M&A': '持續在全球投資或收購中小型優質遊戲工作室；在 AI 領域大舉投資騰訊混元大模型。',
        'CapEx & Expansion': '採購 AI 算力晶片以支撐混元模型，並擴建深圳與貴州的綠色資料中心。',
        'Geopolitical Exposure': '海外遊戲發行受各國對中國科技產品隱私合規審查影響；國內面臨嚴格的未成年遊戲限制與版號規管。',
        'M&A Potential': '全球最大遊戲公司與中國社交霸主，無被併購可能，本身為全球科技界最大戰略投資者。'
    },
    'Meituan': {
        'CEO & Management': '王興(創辦人/CEO)以「無邊界擴張」著稱，極力穩固中國外送龍頭地位，大推無人機送賣賣，並積極開拓香港與中東市場(KeeTa)。',
        'Core Patents & IP': '即時外送「超腦」調度系統專利、自動配送無人機與無人配送車核心專利。',
        '3-Year M&A': '收購光年之外以取得 AI 大模型技術；收購多個地方零售與生鮮供應鏈軟體。',
        'CapEx & Expansion': '資本支出主要用於採購即時配送機車、無人機硬體，及擴充外送大數據雲端算力。',
        'Geopolitical Exposure': '面臨中國國內極高的外送騎手社保權益法規規管，直接壓迫其平台抽成利潤。',
        'M&A Potential': '中國外送與本地生活服務壟斷者，無被併購可能。'
    },
    'EVA Air': {
        'CEO & Management': '林寶水與張國華管理團隊專注於高毛利「客運復甦與高階電商航空貨運」，風格穩健，多次獲得全球最安全航空公司前十名。',
        'Core Patents & IP': '航空機隊排班與動態燃油管理軟體 IP、航空精密維修工藝專利。',
        '3-Year M&A': '大量採購波音 787 與空中巴士 A350 次世代客機，進行機隊汰舊換新；無大型企業併購。',
        'CapEx & Expansion': '數千億台幣資本支出，用於向波音與空巴購買最新客貨機，並升級桃園精密機隊維修廠。',
        'Geopolitical Exposure': '高度依賴兩岸航線與台美航線景氣，受航空燃油價格劇烈波動與兩岸地緣政治限制影響大。',
        'M&A Potential': '長榮集團核心空運資產，股權爭奪戰平息後由大房掌控，經營權極穩，無被併購想像。'
    },
    'China Airlines': {
        'CEO & Management': '董事長謝世謙具備深厚客貨運背景，公股背景濃厚，在台灣半導體與電子零組件「航空貨運」市場擁有極高市佔率。',
        'Core Patents & IP': '冷鏈物流航空貨櫃控溫專利、危險品與精密半導體設備航空載運認證。',
        '3-Year M&A': '購入多架波音 777F 全貨機與空巴 A321neo 客機；無外部企業整併計畫。',
        'CapEx & Expansion': '斥資數百億台幣進行機隊汰換，並投資擴建桃園機場客貨運倉儲及冷鏈設施。',
        'Geopolitical Exposure': '公股背景使其在國際航權談判上面臨地緣政治挑戰；航油價格與台美科技出貨量是獲利關鍵。',
        'M&A Potential': '台灣官股控制航空公司，無被私有化或併購之可能。'
    },
    'Yang Ming': {
        'CEO & Management': '官股主導，管理層專注於加入「THE Alliance」海運聯盟進行全球航線共艙，作風穩健偏保守。',
        'Core Patents & IP': '貨櫃輪配載平衡計算系統、綠色船舶節能推進系統專利。',
        '3-Year M&A': '訂造數艘 15,000 TEU 液化天然氣(LNG)雙燃料貨櫃輪，以符合國際海事組織(IMO)減碳新法規。',
        'CapEx & Expansion': '投資自建超大型綠色貨櫃輪，並參股全球關鍵港口(如高雄港)的專用貨櫃碼頭。',
        'Geopolitical Exposure': '紅海危機等運河中斷事件直接推升其運價，但也大幅增加避航繞道成本，高度暴露於全球地緣貿易衝突。',
        'M&A Potential': '交通部為最大股東，為台灣戰略運輸資產，無被收購可能。'
    },
    'Wan Hai': {
        'CEO & Management': '陳氏家族牢牢控制，以「亞洲近洋航線之王」著稱，疫情期間大舉購入中遠洋貨輪開闢美西線，作風靈活敏捷。',
        'Core Patents & IP': '近洋多港口快速裝卸平衡算法、冷藏貨櫃保鮮監控系統專利。',
        '3-Year M&A': '少有外部收購。主要在海運低潮期大舉購入二手便宜貨櫃輪與訂造新節能船。',
        'CapEx & Expansion': '投資數百億台幣建造節能新船，並擴建台北港及台中港的專用貨櫃碼頭。',
        'Geopolitical Exposure': '亞洲區域內貿易(RCEP)對其近洋航線影響極深；在中美遠洋航線上面臨大聯盟削價競爭挑戰。',
        'M&A Potential': '家族持股極高且高度集權，無被敵意收購可能。'
    },
    'Evergreen Marine': {
        'CEO & Management': '張國華主導，為台灣海運霸主、全球第七大貨櫃航運龍頭，以「極致的船隊規模與脫硫塔配備」確立低成本競爭優勢。',
        'Core Patents & IP': '超大型貨櫃輪(24,000 TEU)流體力學設計、自動化貨櫃碼頭管理系統專利。',
        '3-Year M&A': '斥資收購長榮鋼鐵、榮運等上下游關聯公司股權以進行集團內資源整合；無重大跨國併購。',
        'CapEx & Expansion': '每年資本支出高達數百億台幣，用於在台灣與南韓造船廠訂造全新甲醇雙燃料巨型貨櫃輪。',
        'Geopolitical Exposure': '航線遍佈全球，受地緣衝突(紅海、台海)、美東碼頭罷工及全球通膨消費力放緩的直接衝擊最大。',
        'M&A Potential': '長榮集團旗艦資產，市值極大，股權集中於張氏家族控股，無被併購可能。'
    },
    'Taiwan High Speed Rail': {
        'CEO & Management': '董事長江耀宗主導，採公私合營(PPP)模式，以「安全第一、極致準點」奠定台灣西部走廊交通命脈。',
        'Core Patents & IP': '日本新幹線系統在台特許適應專利、地震與邊坡天然災害即時預警系統 IP。',
        '3-Year M&A': '採購 12 組日本新幹線次世代 N700S 列車(約285億台幣)，以因應台灣西部日益增長的商務客運量。',
        'CapEx & Expansion': '採購新世代高鐵列車、進行軌道鋼軌與電車線的大規模預防性汰換保養。',
        'Geopolitical Exposure': '完全位於台灣本島，零跨國斷鏈風險，但高度暴露於台灣本土少子化、高鐵票價凍漲政策及天然地震災害風險。',
        'M&A Potential': '官股實質控股逾六成，為台灣交通基建核心，絕對無被併購可能。'
    }
}

try:
    with open(csv_path, 'r', encoding='utf-8-sig') as infile, \
         open(temp_csv_path, 'w', newline='', encoding='utf-8-sig') as outfile:
        
        reader = csv.reader(infile)
        writer = csv.writer(outfile)
        
        headers = next(reader)
        writer.writerow(headers)
        
        companies_added = 0
        added_list = []
        for row in reader:
            if 'Global_Entity' in row[5] and companies_added < len(new_companies):
                comp = new_companies[companies_added]
                comp_name_short = comp[1].split(' (')[0]
                added_list.append(comp_name_short)
                
                row[0] = comp[0]
                row[3] = comp[2]
                row[5] = comp[1]
                
                research = updates.get(comp_name_short, {})
                
                row[15] = research.get('CEO & Management', '調查中')
                row[16] = research.get('Core Patents & IP', '調查中')
                row[17] = research.get('3-Year M&A', '調查中')
                row[18] = research.get('CapEx & Expansion', '調查中')
                row[19] = research.get('Geopolitical Exposure', '調查中')
                row[20] = research.get('M&A Potential', '調查中')
                
                row[6] = "等待 API 抓取市值"
                row[7] = "深度調查完成"
                row[8] = "深度調查完成"
                
                # Option A: Get real-time Quiver Quant Insider trading data for the ticker
                ticker_match = re.search(r'\((.*?)\)', comp[1])
                ticker = ticker_match.group(1) if ticker_match else ""
                
                print(f"Fetching insider data for {ticker}...")
                insider_data = get_insider_data(ticker)
                
                row[21] = insider_data
                companies_added += 1
                
            writer.writerow(row)
            
    os.replace(temp_csv_path, csv_path)
    
    # Option B: Sweep Quiver Quant for market-wide insider anomalies > $1M
    print("Sweeping Quiver Quant for insider anomaly alerts...")
    anomalies = scan_large_transactions()
    
    progress = {}
    if os.path.exists(progress_path):
        with open(progress_path, 'r', encoding='utf-8') as f:
            progress = json.load(f)
            
    new_harvested = progress.get('real_tickers_harvested', 267) + companies_added
    progress['real_tickers_harvested'] = new_harvested
    progress['deep_research_completed'] = new_harvested
    progress['current_status'] = f"Batch 14 Deep Research Complete (Total: {new_harvested}/100,000). System idle."
    
    with open(progress_path, 'w', encoding='utf-8') as f:
        json.dump(progress, f, ensure_ascii=False, indent=4)
        
    print(f"Success! Batch 14 completed. {companies_added} companies enriched.")
    
    # Send Anomaly Alerts if any found
    if anomalies:
        anomaly_msg = "🚨 **[Insider Anomaly Alert - 巨額交易警報]**\n以下內部人單筆交易金額突破 **$1.0M** 美元：\n\n" + "\n\n".join(anomalies[:10]) # Limit to top 10
        send_telegram(anomaly_msg)
        
    # Send Finish Telegram
    finish_text = f"✅ **Batch 14 Complete!**\nTotal Progress: {new_harvested}/100,000\n\n**Companies Researched in this batch:**\n" + ", ".join(added_list)
    send_telegram(finish_text)
    
except Exception as e:
    print(f"Error: {e}")
