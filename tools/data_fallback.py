# -*- coding: utf-8 -*-
"""
数据源适配器 - 当东方财富 push2/push2his API 不可用时使用备用数据源
备用数据源:
  - 股票列表: datacenter-web.eastmoney.com (RPT_LICO_FN_CPD)
  - 实时行情: 腾讯行情API (qt.gtimg.cn)
  - K线数据: 腾讯K线API (web.ifzq.gtimg.cn)

使用方式 (monkey-patch):
  import data_fallback
  data_fallback.patch_module(stock_screener)
  stock_screener.main()
"""
import requests
import time
import json

# === 请求头 ===
_HEADERS_EM = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://data.eastmoney.com/'
}
_HEADERS_TX = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

# === 腾讯行情字段索引 (基于 qt.gtimg.cn 返回格式) ===
# 格式: v_sh600000="1~名称~代码~当前价~昨收~今开~成交量~..."
TX_FIELD_PRICE = 3        # 当前价格
TX_FIELD_PREV_CLOSE = 4   # 昨收价
TX_FIELD_CHANGE_PCT = 32  # 涨跌幅(%)
TX_FIELD_PE = 39          # PE(动态)
TX_FIELD_TOTAL_MV = 45    # 总市值(亿元)
TX_FIELD_CIRC_MV = 44     # 流通市值(亿元)


def _tx_code(code):
    """将股票代码转为腾讯格式 (sh600000 / sz000001)"""
    if code.startswith(('6', '9')):
        return f'sh{code}'
    return f'sz{code}'


def fallback_get_all_stocks():
    """
    获取全A股列表 (datacenter + 腾讯行情)
    返回格式与原 get_all_stocks() 完全一致:
      [{"code", "name", "price", "change_pct", "market_cap", "pe", "industry"}, ...]
    """
    print("  [备用数据源] datacenter-web + 腾讯行情", flush=True)

    # === 步骤1: 从 datacenter 获取股票基础列表 ===
    dc_url = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
    stock_list = []  # [(code, name, board), ...]
    page = 1
    seen_codes = set()

    while True:
        params = {
            'sortColumns': 'SECURITY_CODE', 'sortTypes': 1,
            'pageSize': 500, 'pageNumber': page,
            'reportName': 'RPT_LICO_FN_CPD',
            'columns': 'SECURITY_CODE,SECURITY_NAME_ABBR,SECUCODE,TRADE_MARKET,BOARD_NAME,ISNEW',
            'filter': '(ISNEW=1)'
        }
        try:
            r = requests.get(dc_url, params=params, headers=_HEADERS_EM, timeout=30)
            data = r.json().get('result', {})
            items = data.get('data', [])
        except Exception:
            items = []

        if not items:
            break

        for item in items:
            code = item.get('SECURITY_CODE', '')
            name = item.get('SECURITY_NAME_ABBR', '')
            board = item.get('BOARD_NAME', '') or ''

            # 只保留A股代码 (6/0/3/688开头)，排除B股/基金/债券等
            if not code or not code[0].isdigit():
                continue
            if code[0] not in ('6', '0', '3'):
                continue
            # 排除指数
            if code in ('000001',) and name == '上证指数':
                continue
            if code in seen_codes:
                continue
            seen_codes.add(code)
            stock_list.append((code, name, board))

        if len(items) < 500:
            break
        page += 1
        if page % 5 == 0:
            print(f"  [datacenter] 已获取 {len(stock_list)} 只...", flush=True)

    print(f"  [datacenter] 共 {len(stock_list)} 只A股", flush=True)

    # === 步骤2: 用腾讯行情API批量获取实时数据 ===
    stocks = []
    batch_size = 60  # 每次获取60只 (URL长度安全)
    total_batches = (len(stock_list) + batch_size - 1) // batch_size

    # 构建 code -> board 映射
    code_board = {c: b for c, n, b in stock_list}

    for batch_idx in range(total_batches):
        start = batch_idx * batch_size
        end = min(start + batch_size, len(stock_list))
        batch = stock_list[start:end]

        tx_codes = [_tx_code(c) for c, n, b in batch]
        tx_url = f'http://qt.gtimg.cn/q={",".join(tx_codes)}'

        try:
            r = requests.get(tx_url, headers=_HEADERS_TX, timeout=15)
            r.encoding = 'gbk'
            text = r.text
        except Exception:
            time.sleep(0.5)
            continue

        for line in text.strip().split(';'):
            line = line.strip()
            if not line or '=' not in line:
                continue
            try:
                val = line.split('=', 1)[1].strip(';').strip('"')
                parts = val.split('~')
                if len(parts) < 50:
                    continue

                code = parts[2]
                name = parts[1]

                # 解析数值字段
                def _safe_float(s, default=0):
                    try:
                        return float(s) if s and s != '' else default
                    except (ValueError, TypeError):
                        return default

                price = _safe_float(parts[TX_FIELD_PRICE])
                change_pct = _safe_float(parts[TX_FIELD_CHANGE_PCT])
                total_mv_yi = _safe_float(parts[TX_FIELD_TOTAL_MV])  # 单位: 亿
                market_cap = total_mv_yi * 1e8  # 转为元
                pe = _safe_float(parts[TX_FIELD_PE])

                stocks.append({
                    'code': code,
                    'name': name,
                    'price': price,
                    'change_pct': change_pct,
                    'market_cap': market_cap,
                    'pe': pe,
                    'industry': code_board.get(code, ''),
                })
            except Exception:
                continue

        if (batch_idx + 1) % 10 == 0:
            print(f"  [腾讯行情] 已获取 {len(stocks)}/{len(stock_list)} 只...", flush=True)
        time.sleep(0.15)

    print(f"  [腾讯行情] 共获取 {len(stocks)} 只股票实时数据", flush=True)
    return stocks


def _get_klines_tencent(code, days):
    """腾讯K线API获取前复权日K线 (1次快速尝试, 失败则降级)"""
    prefix = 'sh' if code.startswith(('6', '9')) else 'sz'
    tx_symbol = f'{prefix}{code}'
    fetch_count = days + 50
    url = 'http://web.ifzq.gtimg.cn/appstock/app/fqkline/get'

    try:
        params = {'param': f'{tx_symbol},day,,,{fetch_count},qfq'}
        r = requests.get(url, params=params, headers=_HEADERS_TX, timeout=8)
        data = r.json()
        stock_data = data.get('data', {}).get(tx_symbol, {})
        klines = stock_data.get('day', stock_data.get('qfqday', []))
        if klines:
            return klines, tx_symbol, url
    except Exception:
        pass
    return None, tx_symbol, url


def _get_klines_sina(code, days):
    """新浪K线API获取日K线 (不复权, 作为降级方案)"""
    prefix = 'sh' if code.startswith(('6', '9')) else 'sz'
    sina_symbol = f'{prefix}{code}'
    url = 'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData'
    params = {'symbol': sina_symbol, 'scale': '240', 'datalen': str(days + 50), 'ma': 'no'}

    for attempt in range(2):
        try:
            r = requests.get(url, params=params, headers=_HEADERS_TX, timeout=12)
            if r.status_code == 200 and r.text.strip():
                import json as _json
                data = _json.loads(r.text)
                if data:
                    return data
        except Exception:
            pass
        time.sleep(0.3 * (attempt + 1))
    return None


def fallback_get_klines(code, days=60, adjust_volume=True):
    """
    获取日K线数据 (腾讯K线API优先, 新浪API降级)
    返回格式与原 get_klines() 一致:
      [{"date", "open", "close", "high", "low", "volume", "amount"}, ...]
    """
    fetch_count = days + 50

    # 方案1: 腾讯K线API (前复权)
    klines_tx, tx_symbol, tx_url = _get_klines_tencent(code, days)

    if klines_tx:
        result = []
        for k in klines_tx:
            if len(k) < 6:
                continue
            try:
                result.append({
                    'date': k[0],
                    'open': float(k[1]),
                    'close': float(k[2]),
                    'high': float(k[3]),
                    'low': float(k[4]),
                    'volume': float(k[5]),
                    'amount': float(k[5]) * float(k[2]) if float(k[2]) > 0 else 0,
                })
            except (ValueError, IndexError):
                continue

        # 成交量复权
        if adjust_volume and result:
            try:
                params_raw = {'param': f'{tx_symbol},day,,,{fetch_count},'}
                r2 = requests.get(tx_url, params=params_raw, headers=_HEADERS_TX, timeout=15)
                data2 = r2.json()
                stock_data2 = data2.get('data', {}).get(tx_symbol, {})
                klines_raw = stock_data2.get('day', stock_data2.get('qtday', []))
                if klines_raw and len(klines_raw) == len(result):
                    for i, k in enumerate(klines_raw):
                        if len(k) >= 3:
                            raw_close = float(k[2])
                            if raw_close > 0 and result[i]['close'] > 0:
                                factor = result[i]['close'] / raw_close
                                result[i]['volume'] = result[i]['volume'] * factor
                                result[i]['amount'] = result[i]['amount'] * factor
            except Exception:
                pass

        if result:
            return result[-days:]

    # 方案2: 新浪K线API (降级, 不复权)
    klines_sina = _get_klines_sina(code, days)
    if klines_sina:
        result = []
        for k in klines_sina:
            try:
                vol = float(k.get('volume', 0))
                close = float(k.get('close', 0))
                result.append({
                    'date': k.get('day', ''),
                    'open': float(k.get('open', 0)),
                    'close': close,
                    'high': float(k.get('high', 0)),
                    'low': float(k.get('low', 0)),
                    'volume': vol / 100 if vol > 1000 else vol,  # 新浪单位是股, 转为手
                    'amount': vol * close if close > 0 else 0,
                })
            except (ValueError, TypeError):
                continue
        if result:
            return result[-days:]

    return []


def fallback_get_stock_info(code):
    """
    获取个股信息 (腾讯行情API)
    返回: {"sector", "concepts", "market_cap", "pe"}
    """
    prefix = 'sh' if code.startswith(('6', '9')) else 'sz'
    try:
        r = requests.get(f'http://qt.gtimg.cn/q={prefix}{code}', headers=_HEADERS_TX, timeout=10)
        r.encoding = 'gbk'
        parts = r.text.split('=', 1)[1].strip(';').strip('"').split('~')
        if len(parts) < 50:
            return {'sector': '', 'concepts': '', 'market_cap': 0, 'pe': 0}

        def _safe_float(s, default=0):
            try:
                return float(s) if s and s != '' else default
            except (ValueError, TypeError):
                return default

        return {
            'sector': '',  # 腾讯行情不直接提供行业
            'concepts': '',
            'market_cap': _safe_float(parts[TX_FIELD_TOTAL_MV]) * 1e8,
            'pe': _safe_float(parts[TX_FIELD_PE]),
        }
    except Exception:
        return {'sector': '', 'concepts': '', 'market_cap': 0, 'pe': 0}


def fallback_get_big_order_net(code, dates, days=120):
    """主力净流入数据 - 备用源不可用，返回空"""
    return {}


def fallback_get_risk_warnings(code):
    """风险公告 - 备用源不可用，返回空"""
    return []


def patch_module(ss_module):
    """
    Monkey-patch stock_screener 模块，替换所有东方财富API依赖函数
    """
    ss_module.get_all_stocks = fallback_get_all_stocks
    ss_module.get_klines = fallback_get_klines
    ss_module.get_stock_info = fallback_get_stock_info
    ss_module.get_big_order_net = fallback_get_big_order_net
    ss_module.get_risk_warnings = fallback_get_risk_warnings
    print("=" * 60)
    print("  数据源已切换为备用模式 (datacenter + 腾讯行情 + 腾讯K线)")
    print("=" * 60)
