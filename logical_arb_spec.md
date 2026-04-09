# Kalshi Logical Arbitrage Scanner — Technical Specification

## Overview

This bot scans all ~7,000+ open Kalshi markets for pricing violations that constitute risk-free or near-risk-free arbitrage. Unlike cross-exchange arb, these are *intra-Kalshi* logical inconsistencies where prices violate the mathematical rules of probability.

---

## 1. Kalshi API Architecture

### Base URL
```
Production: https://api.elections.kalshi.com/trade-api/v2
Demo:       https://demo-api.kalshi.co/trade-api/v2
```

### Authentication
RSA-PSS signed headers on every request:
```
KALSHI-ACCESS-KEY:       <api_key_id>
KALSHI-ACCESS-SIGNATURE: base64(RSA-PSS-SHA256(timestamp + METHOD + path_no_query))
KALSHI-ACCESS-TIMESTAMP: unix_ms
```

### Rate Limits by Tier

| Tier     | Read req/s | Write req/s | Qualification                       |
|----------|-----------|-------------|-------------------------------------|
| Basic    | 20        | 10          | Automatic on signup                 |
| Advanced | 30        | 30          | Application form                    |
| Premier  | 100       | 100         | 3.75% of monthly exchange volume    |
| Prime    | 400       | 400         | 7.5% of monthly exchange volume     |

Write = CreateOrder, CancelOrder, BatchCreateOrders (each order in batch = 1 write unit, except BatchCancel where each = 0.2).

### Key Endpoints

| Purpose                    | Method | Endpoint                                   |
|----------------------------|--------|--------------------------------------------|
| List all markets           | GET    | `/markets`                                 |
| Get single market          | GET    | `/markets/{ticker}`                        |
| List all events (w/ markets)| GET   | `/events?with_nested_markets=true`         |
| List series                | GET    | `/series`                                  |
| Get orderbook              | GET    | `/markets/{ticker}/orderbook`              |
| Place order                | POST   | `/portfolio/orders`                        |
| Batch place orders         | POST   | `/portfolio/orders/batched`                |
| Cancel order               | DELETE | `/portfolio/orders/{order_id}`             |
| WebSocket stream           | WSS    | `wss://api.elections.kalshi.com/`          |

---

## 2. Market & Event Structure

### Hierarchy
```
Series (template, e.g. "KXBTC")
  └── Event (e.g. "KXBTC-25JAN")
        └── Markets (e.g. "KXBTC-25JAN-B50000", "KXBTC-25JAN-B60000")
```

### Market Object — Key Fields
```json
{
  "ticker":          "KXBTC-25APR-B50000",
  "event_ticker":    "KXBTC-25APR",
  "series_ticker":   "KXBTC",
  "title":           "Will BTC be above $50,000?",
  "market_type":     "binary",
  "status":          "open",
  "yes_bid_dollars": "0.5800",
  "yes_ask_dollars": "0.6000",
  "no_bid_dollars":  "0.3800",
  "no_ask_dollars":  "0.4200",
  "last_price_dollars": "0.5900",
  "floor_strike":    50000,
  "cap_strike":      null,
  "strike_type":     "greater",
  "mutually_exclusive": false,
  "volume_fp":       "12500.00"
}
```

### Event `mutually_exclusive` Field
- `true` → only ONE market in this event can resolve YES (e.g. "which candidate wins?", ranged buckets like "<2%", "2-4%", ">4%")
- `false` → markets can resolve independently

### YES/NO Price Relationship (Critical)
```
yes_ask + no_ask  ≈ 1.00 + spread  (never tradeable above $1 payout)
yes_bid + no_bid  ≈ 1.00 - spread  (both sides, bid side)

A bid for YES at price X  ≡  an ask for NO at price (1 - X)
```
The orderbook only returns bids. To construct the full picture:
- Best YES ask = 1 - best NO bid
- Best NO ask = 1 - best YES bid

---

## 3. Fee Structure

### Taker Fee (fills against resting orders)
```
taker_fee = ceil(0.07 × C × P × (1 - P))
```
- C = number of contracts
- P = execution price in dollars (0.01 to 0.99)
- Max fee: 1.75¢/contract at P=0.50
- Example: 100 contracts at 0.49¢ → fee = ceil(0.07 × 100 × 0.49 × 0.51) = $1.75

### Maker Fee (resting limit orders when matched)
```
maker_fee = ceil(0.0175 × C × P × (1 - P))
```
- Exactly 25% of taker fee (changed July 2025)
- Example: 100 contracts at 0.49¢ → fee = $0.44

### Index/Equity Markets (S&P 500, Nasdaq)
- Fee multiplier halved: `0.035` instead of `0.07`

### Fee Rebate Program (LIP)
- Active through January 27, 2027
- Rebates up to 1% of exchange fees paid monthly (for non-intermediated trading)
- Tiered — details in CFTC filing; effectively maker fee approaches zero for high-volume traders

### Net Profit Formula for Arb
```
gross_profit = payout - total_cost_of_legs
total_fees   = sum(fee_per_leg_based_on_execution_type)
net_profit   = gross_profit - total_fees
```

For the arb to be worth executing:
```
net_profit > 0  AND  net_profit / capital_at_risk > threshold (e.g. 0.5%)
```

---

## 4. The Three Arbitrage Types

### Type A: Monotone Containment Violation
**Logic:** If event A logically implies event B, then P(B) ≥ P(A) always.

**Example:**
- Market 1: "Will BTC close above $50,000?" — YES ask = 0.60
- Market 2: "Will BTC close above $60,000?" — YES ask = 0.65

This is impossible. If BTC is above $60k, it is necessarily above $50k, so P(above 50k) ≥ P(above 60k). The $60k YES is overpriced relative to $50k YES.

**Arbitrage:** Sell YES $60k (buy NO $60k at 0.35) + hold cash. Or more precisely: buy YES $50k + sell YES $60k via paired positions.

**Detection:**
1. Group all "above $X" markets in same event by ascending strike
2. Compute YES prices: [P_50k, P_60k, P_70k, ...]
3. Flag any index i where `YES_price[i] > YES_price[i-1]` (higher strike has higher yes price = violation)

### Type B: Mutually Exclusive + Exhaustive Sum ≠ 100¢
**Logic:** If markets are mutually exclusive and exhaustive (exactly one must win), their YES prices must sum to exactly 100¢.

**Example (GDP growth buckets):**
- "<2%" YES = 35¢
- "2%-4%" YES = 30¢
- ">4%" YES = 20¢
- Sum = 85¢ → 15¢ of free money

**Arbitrage:** Buy YES on all buckets for 85¢ total → guaranteed $1.00 payout.
- Net profit = $1.00 - $0.85 - total_fees = $0.15 - fees

**Detection:**
1. Fetch all events with `mutually_exclusive=true`
2. For each, fetch all child markets
3. Sum all YES asks
4. If sum < 100¢ → buy-all opportunity
5. If sum > 100¢ → sell-all opportunity (buy all NOs, sum should be < 100¢)

### Type C: Binary YES + NO Sum Violation
**Logic:** YES and NO of the same market cover all outcomes, so:
`P(YES) + P(NO) = 1.00` exactly at equilibrium.

In practice, `YES_ask + NO_ask > 1.00` (the spread). But if someone misprices:
- `YES_ask + NO_ask < 1.00` → buy both for guaranteed payout of $1.00
- Extremely rare within a single market but worth checking

**Detection:**
```python
if yes_ask + no_ask < 1.00:
    profit = 1.00 - yes_ask - no_ask - fees
    if profit > 0: execute
```

---

## 5. Detection Algorithm

### Step 1: Full Market Scan
```python
# Fetch all open events with nested markets (~7,000 markets in ~500+ events)
# GET /events?status=open&with_nested_markets=true&limit=200
# Paginate via cursor until cursor is None
# Also fetch multivariate events separately via GET /events/multivariate

# Group by event_ticker, then by series_ticker
event_map = {}  # event_ticker -> list of markets
series_map = {} # series_ticker -> list of events
```

### Step 2: Classify Event Types
```python
for event in events:
    markets = event['markets']

    # Type B: mutually exclusive bucket events
    if event['mutually_exclusive']:
        check_exhaustive_sum(markets)

    # Type A: monotone containment (step/threshold markets)
    if has_numeric_strikes(markets):
        check_monotone_constraint(markets)

    # Type C: within-market (run on every market)
    for market in markets:
        check_binary_sum(market)
```

### Step 3: Monotone Constraint Check
```python
def check_monotone_constraint(markets):
    """
    For 'above X' markets, sort by floor_strike ascending.
    YES price must be non-increasing as strike increases.
    """
    # Filter to 'above' type (strike_type == 'greater')
    above_markets = [m for m in markets if m.get('strike_type') == 'greater']
    above_markets.sort(key=lambda m: m['floor_strike'])

    violations = []
    for i in range(1, len(above_markets)):
        lower_strike = above_markets[i-1]  # e.g. $50k
        higher_strike = above_markets[i]   # e.g. $60k

        # Get executable prices (taker asks)
        p_lower = float(lower_strike['yes_ask_dollars'])  # cost to buy YES $50k
        p_higher = float(higher_strike['yes_ask_dollars']) # cost to buy YES $60k

        if p_higher > p_lower:
            # Violation! $60k YES costs more than $50k YES
            # Trade: Buy YES $50k (at p_lower), Sell YES $60k (buy NO $60k)
            # NO $60k ask = 1 - YES $60k bid (check orderbook for real NO ask)
            no_higher_ask = 1.0 - float(higher_strike['yes_bid_dollars'])

            total_cost = p_lower + no_higher_ask  # cost of both legs
            payout = 1.0  # if BTC < $60k: YES $50k loses ($0), NO $60k wins ($1); net $1
                          # if BTC > $60k: YES $50k wins ($1), NO $60k loses ($0); net $1
                          # Wait — need careful payout analysis per scenario

            # Corrected: these are NOT perfectly paired positions
            # Strategy depends on mispricing magnitude — see Section 6
            violations.append({
                'type': 'monotone_violation',
                'buy_market': lower_strike['ticker'],
                'sell_market': higher_strike['ticker'],
                'buy_yes_ask': p_lower,
                'sell_yes_bid': float(higher_strike['yes_bid_dollars']),
                'gross_edge': p_higher - p_lower,
                'event_ticker': lower_strike['event_ticker']
            })

    return violations
```

### Step 4: Exhaustive Sum Check
```python
def check_exhaustive_sum(markets):
    """
    For mutually exclusive + exhaustive events, YES prices must sum to $1.00.
    Buy-all arb if sum < $1.00.
    """
    open_markets = [m for m in markets if m['status'] == 'open']

    yes_asks = [float(m['yes_ask_dollars']) for m in open_markets]
    total_ask = sum(yes_asks)

    if total_ask < 1.00:
        fees_estimate = sum(
            calc_taker_fee(1, p) for p in yes_asks
        )
        net_profit = (1.00 - total_ask - fees_estimate)
        if net_profit > 0.005:  # 0.5¢ minimum threshold
            return {
                'type': 'exhaustive_underpriced',
                'markets': [m['ticker'] for m in open_markets],
                'yes_asks': yes_asks,
                'total_cost': total_ask,
                'estimated_fees': fees_estimate,
                'net_profit_per_contract': net_profit
            }

    # Also check sell-all (sum of NO asks)
    no_asks = [1.0 - float(m['yes_bid_dollars']) for m in open_markets]
    total_no_ask = sum(no_asks)
    if total_no_ask < 1.00:
        # Buy all NOs
        fees_estimate = sum(calc_taker_fee(1, p) for p in no_asks)
        net_profit = 1.00 - total_no_ask - fees_estimate
        if net_profit > 0.005:
            return {
                'type': 'exhaustive_no_underpriced',
                'markets': [m['ticker'] for m in open_markets],
                'no_asks': no_asks,
                'net_profit_per_contract': net_profit
            }
```

---

## 6. Execution Strategy

### Monotone Containment Trade
For "BTC above $50k YES" mispriced vs "BTC above $60k YES":

| Scenario       | YES $50k | NO $60k | Net   |
|----------------|----------|---------|-------|
| BTC < $50k     | Loses $1 | Wins $1 | $0    |
| $50k < BTC < $60k | Wins $1 | Wins $1 | +$2 (cost both ≈ $1, profit!) |
| BTC > $60k     | Wins $1  | Loses $1| $0    |

Wait — the middle scenario is the edge. If BTC ends between $50k and $60k, you win BOTH. So the position costs (YES $50k ask) + (NO $60k ask). If total < $1.00, you have a risk-free profit in the middle scenario and break even on extremes. This is NOT fully risk-free unless cost < $0.

The true arb is: YES $60k bid > YES $50k ask (price inversion).

**Actual Trade when P($60k YES) > P($50k YES):**
- Sell YES $60k at the inflated bid (or at ask as a taker on the other side)
- Buy YES $50k at the deflated ask
- Net: if prices converge, you close both at fair value and capture the spread

This is a **relative value trade**, not a lock-in arb unless the violation is severe enough that:
```
YES $50k ask + NO $60k ask < $1.00
```
Because then: buy YES $50k + buy NO $60k for < $1. You win both legs in middle scenario, which guarantees profit (you can't lose money on extremes if cost ≤ $1.00 and you win $1 on each extreme).

**Execution for lock-in:**
1. Simultaneously place limit orders: buy YES $50k, buy NO $60k
2. Use `order_group_id` to link orders
3. If total fill cost < $1.00, guaranteed profit at settlement

### Exhaustive Sum Trade
Straightforward: buy 1 contract YES on every bucket.
- Use BatchCreateOrders (up to 20 per batch, sequential not simultaneous)
- Race condition risk: prices move between leg 1 and leg N

**Mitigation:**
- Pre-check sum including estimated market impact
- Set `time_in_force: fill_or_kill` on first leg (most expensive)
- Use limit orders at ask price (post_only=false) for immediate fills
- Build in cancel logic: if any leg fails, cancel all filled legs

### Order Parameters for Arb
```python
order = {
    "ticker": "KXBTC-25APR-B50000",
    "action": "buy",
    "side": "yes",
    "count": N,
    "yes_price_dollars": "0.5800",     # limit price (the ask)
    "time_in_force": "fill_or_kill",   # don't leave partial open
    "client_order_id": str(uuid.uuid4()),
    "order_group_id": "arb_group_001"  # link related legs
}
```

---

## 7. Position Sizing & Risk

### Fee Impact on Profitability
At 50¢ contracts (worst case):
- Taker fee = 1.75¢/contract
- 2-leg arb = 3.50¢ in fees
- Minimum gross edge needed = 3.50¢ per contract

At 20¢ or 80¢ contracts:
- Taker fee = ceil(0.07 × 0.20 × 0.80) = 1.12¢/contract
- 2-leg = 2.24¢ fees

Using maker orders cuts fees to 25% (0.44¢ at 50¢). Total 2-leg maker = 0.88¢. But maker orders carry adverse selection risk and execution risk.

**Recommendation:** Use makers for the "stable" leg (the one that's correctly priced) and taker for the mispriced leg to guarantee entry.

### Minimum Viable Edge
```python
MIN_EDGE_CENTS = 0.50  # 0.5¢ net after fees, per contract
MIN_NOTIONAL   = 5.00  # $5 minimum to bother executing
MAX_CONTRACTS_PER_LEG = 100  # limits slippage
```

### Capital Allocation
- Allocate at most 10% of account balance to any single arb position
- Prefer arbs where settlement is close (< 7 days) to limit time risk

### Risks
1. **Execution lag:** BatchCreateOrders is sequential, not atomic. Prices can move between legs.
2. **Thin books:** Low volume markets may not fill at ask. Use orderbook depth check.
3. **Market pause:** Kalshi can pause markets mid-trade. Use `cancel_order_on_pause=true`.
4. **Fees erode edge:** Always calculate net profit post-fees before entering.
5. **Contract count rounding:** Fees use ceiling, so small counts are penalized.
6. **Logical but not contractual:** Verify settlement rules in `rules_primary` to confirm true logical containment (some "above X" markets have different settlement sources).

---

## 8. Real-Time vs. Polling Architecture

### Option A: REST Polling (Simpler, Lower Rate)
```
Every 30s: GET /events?status=open&with_nested_markets=true (all events, paginated)
On violation found: fetch orderbook for depth check → place orders
```
At 20 req/s (Basic tier), fetching 7,000 markets via /events with nested (200 per page = 35 pages) takes ~35 requests → completes in ~2s. Full refresh cycle: 30s.

### Option B: WebSocket + Bootstrap (Faster, More Complex)
```
1. Bootstrap: full REST scan on startup
2. Subscribe to `ticker` channel for all open market tickers
3. On each price update: re-check constraints for that market's event group
4. On violation: fetch orderbook → place orders
```
WebSocket `ticker` channel pushes yes_bid/yes_ask changes in real-time. Latency to detection < 100ms.

**Recommended:** Start with Option A (polling), graduate to Option B.

---

## 9. Python Implementation Skeleton

```python
#!/usr/bin/env python3
"""
Kalshi Logical Arbitrage Scanner
Detects and executes: monotone violations, exhaustive sum arbs, binary sum arbs
"""

import asyncio
import base64
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from math import ceil
from typing import Optional
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger("kalshi_arb")

# ─── Config ──────────────────────────────────────────────────────────────────

API_KEY_ID         = os.getenv("KALSHI_API_KEY_ID", "")
PRIVATE_KEY_PEM    = os.getenv("KALSHI_PRIVATE_KEY_PEM", "")
BASE_URL           = "https://api.elections.kalshi.com/trade-api/v2"
PAPER_MODE         = os.getenv("PAPER_MODE", "true").lower() == "true"
MIN_EDGE_CENTS     = float(os.getenv("MIN_EDGE_CENTS", "0.50"))
MIN_NOTIONAL       = float(os.getenv("MIN_NOTIONAL", "5.00"))
POLL_INTERVAL_SEC  = int(os.getenv("POLL_INTERVAL_SEC", "30"))
MAX_CONTRACTS      = int(os.getenv("MAX_CONTRACTS", "50"))
MAX_BALANCE_PCT    = float(os.getenv("MAX_BALANCE_PCT", "0.10"))

# ─── Auth ────────────────────────────────────────────────────────────────────

def load_private_key():
    pem = PRIVATE_KEY_PEM.replace("\\n", "\n").encode()
    return serialization.load_pem_private_key(pem, password=None, backend=default_backend())

def sign_request(private_key, ts_ms: str, method: str, path: str) -> str:
    clean_path = urlparse(path).path
    msg = f"{ts_ms}{method}{clean_path}".encode()
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256()
    )
    return base64.b64encode(sig).decode()

def auth_headers(private_key, method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sign_request(private_key, ts, method, path),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }

# ─── API Client ──────────────────────────────────────────────────────────────

class KalshiClient:
    def __init__(self):
        self.private_key = load_private_key()
        self.http = httpx.AsyncClient(base_url=BASE_URL, timeout=15.0)

    async def get(self, path: str, params: dict = None) -> dict:
        hdrs = auth_headers(self.private_key, "GET", path)
        r = await self.http.get(path, headers=hdrs, params=params)
        r.raise_for_status()
        return r.json()

    async def post(self, path: str, body: dict) -> dict:
        hdrs = auth_headers(self.private_key, "POST", path)
        r = await self.http.post(path, headers=hdrs, json=body)
        r.raise_for_status()
        return r.json()

    async def get_all_events(self) -> list:
        """Paginate through all open events with nested markets."""
        events, cursor = [], None
        while True:
            params = {"status": "open", "with_nested_markets": "true", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = await self.get("/events", params)
            events.extend(data.get("events", []))
            cursor = data.get("cursor")
            if not cursor:
                break
            await asyncio.sleep(0.05)  # 20 req/s = 50ms between calls
        return events

    async def get_orderbook(self, ticker: str) -> dict:
        return await self.get(f"/markets/{ticker}/orderbook")

    async def get_balance(self) -> float:
        data = await self.get("/portfolio/balance")
        return data.get("balance", 0) / 100.0  # cents to dollars

    async def place_order(self, ticker: str, side: str, action: str,
                          count: int, price_dollars: float,
                          group_id: str = None) -> dict:
        body = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "yes_price_dollars" if side == "yes" else "no_price_dollars":
                f"{price_dollars:.4f}",
            "time_in_force": "fill_or_kill",
            "client_order_id": str(uuid.uuid4()),
            "cancel_order_on_pause": True,
        }
        if group_id:
            body["order_group_id"] = group_id
        if PAPER_MODE:
            logger.info(f"[PAPER] ORDER: {body}")
            return {"status": "paper", "order_id": "paper_" + str(uuid.uuid4())}
        return await self.post("/portfolio/orders", body)

# ─── Fee Calculations ─────────────────────────────────────────────────────────

def taker_fee(contracts: int, price: float) -> float:
    """Taker fee in dollars."""
    return ceil(0.07 * contracts * price * (1 - price)) / 100

def maker_fee(contracts: int, price: float) -> float:
    """Maker fee in dollars."""
    return ceil(0.0175 * contracts * price * (1 - price)) / 100

def min_edge_needed(contracts: int, prices: list, use_maker=False) -> float:
    """Total fees for all legs of a trade."""
    fee_fn = maker_fee if use_maker else taker_fee
    return sum(fee_fn(contracts, p) for p in prices)

# ─── Arbitrage Detection ─────────────────────────────────────────────────────

@dataclass
class ArbOpportunity:
    type: str          # 'monotone' | 'exhaustive' | 'binary_sum'
    event_ticker: str
    legs: list         # [{ticker, side, action, price}]
    gross_profit: float
    estimated_fees: float
    net_profit: float
    contracts: int = 1
    notes: str = ""

def detect_monotone_violations(markets: list) -> list[ArbOpportunity]:
    """
    Find: higher strike YES priced above lower strike YES.
    Sort by floor_strike ascending. YES price must be non-increasing.
    """
    opps = []
    above = [m for m in markets
             if m.get("strike_type") == "greater"
             and m.get("status") == "open"
             and m.get("yes_ask_dollars")
             and m.get("yes_bid_dollars")
             and m.get("floor_strike") is not None]

    above.sort(key=lambda m: float(m["floor_strike"]))

    for i in range(1, len(above)):
        low_mkt = above[i-1]  # lower strike (should have higher yes price)
        high_mkt = above[i]   # higher strike (should have lower yes price)

        low_yes_ask  = float(low_mkt["yes_ask_dollars"])
        high_yes_ask = float(high_mkt["yes_ask_dollars"])
        high_yes_bid = float(high_mkt["yes_bid_dollars"])

        if high_yes_ask <= low_yes_ask:
            continue  # no violation

        # Violation: high_yes_ask > low_yes_ask
        # Trade: buy YES lower strike, sell YES higher strike (= buy NO higher strike)
        no_high_ask = 1.0 - high_yes_bid  # cost to buy NO on higher strike

        # Net position cost
        total_cost = low_yes_ask + no_high_ask

        # Payoff matrix:
        # X < low_strike:  YES_low=0, NO_high=1 → payout $1
        # low < X < high:  YES_low=1, NO_high=1 → payout $2
        # X > high:        YES_low=1, NO_high=0 → payout $1
        # Minimum payout = $1, maximum = $2
        # If total_cost < $1.00, locked arb. If $1.00 < total_cost < $2.00, directional edge.

        min_payout = 1.0
        gross = min_payout - total_cost

        if gross <= 0:
            continue  # not a guaranteed arb, just a relative value trade

        fees = taker_fee(1, low_yes_ask) + taker_fee(1, no_high_ask)
        net = gross - fees

        if net >= MIN_EDGE_CENTS / 100:
            opps.append(ArbOpportunity(
                type="monotone",
                event_ticker=low_mkt["event_ticker"],
                legs=[
                    {"ticker": low_mkt["ticker"],  "side": "yes", "action": "buy", "price": low_yes_ask},
                    {"ticker": high_mkt["ticker"], "side": "no",  "action": "buy", "price": no_high_ask},
                ],
                gross_profit=gross,
                estimated_fees=fees,
                net_profit=net,
                notes=f"strike {low_mkt['floor_strike']} vs {high_mkt['floor_strike']}"
            ))
    return opps


def detect_exhaustive_sum(event: dict, markets: list) -> Optional[ArbOpportunity]:
    """
    For mutually_exclusive events: YES prices must sum to $1.00.
    If sum < $1.00, buy all YES contracts.
    """
    if not event.get("mutually_exclusive"):
        return None

    open_mkts = [m for m in markets
                 if m.get("status") == "open"
                 and m.get("yes_ask_dollars")]

    if len(open_mkts) < 2:
        return None

    yes_asks = [float(m["yes_ask_dollars"]) for m in open_mkts]
    total_cost = sum(yes_asks)

    if total_cost >= 1.00:
        return None

    gross = 1.00 - total_cost
    fees = sum(taker_fee(1, p) for p in yes_asks)
    net = gross - fees

    if net < MIN_EDGE_CENTS / 100:
        return None

    return ArbOpportunity(
        type="exhaustive",
        event_ticker=event["event_ticker"],
        legs=[
            {"ticker": m["ticker"], "side": "yes", "action": "buy",
             "price": float(m["yes_ask_dollars"])}
            for m in open_mkts
        ],
        gross_profit=gross,
        estimated_fees=fees,
        net_profit=net,
        notes=f"sum={total_cost:.4f}, {len(open_mkts)} legs"
    )


def detect_binary_sum(market: dict) -> Optional[ArbOpportunity]:
    """
    Within single market: if YES_ask + NO_ask < $1.00, buy both.
    """
    if market.get("status") != "open":
        return None
    yes_ask = market.get("yes_ask_dollars")
    yes_bid = market.get("yes_bid_dollars")
    if not yes_ask or not yes_bid:
        return None

    ya = float(yes_ask)
    no_ask = 1.0 - float(yes_bid)  # implied NO ask

    total_cost = ya + no_ask
    if total_cost >= 1.00:
        return None

    gross = 1.00 - total_cost
    fees = taker_fee(1, ya) + taker_fee(1, no_ask)
    net = gross - fees

    if net < MIN_EDGE_CENTS / 100:
        return None

    return ArbOpportunity(
        type="binary_sum",
        event_ticker=market.get("event_ticker", ""),
        legs=[
            {"ticker": market["ticker"], "side": "yes", "action": "buy", "price": ya},
            {"ticker": market["ticker"], "side": "no",  "action": "buy", "price": no_ask},
        ],
        gross_profit=gross,
        estimated_fees=fees,
        net_profit=net
    )

# ─── Execution ────────────────────────────────────────────────────────────────

async def execute_arb(client: KalshiClient, opp: ArbOpportunity, balance: float):
    """Execute a multi-leg arbitrage opportunity."""
    # Size based on balance and edge
    max_spend = balance * MAX_BALANCE_PCT
    leg_cost_per_contract = sum(leg["price"] for leg in opp.legs)
    contracts = max(1, min(MAX_CONTRACTS, int(max_spend / leg_cost_per_contract)))

    # Recalculate net at actual size
    fees = sum(taker_fee(contracts, leg["price"]) for leg in opp.legs)
    net = (opp.gross_profit * contracts) - fees

    if net < MIN_NOTIONAL:
        logger.debug(f"Skipping {opp.type} arb — net ${net:.4f} below ${MIN_NOTIONAL} minimum")
        return

    logger.info(f"EXECUTING {opp.type} arb | event={opp.event_ticker} | "
                f"contracts={contracts} | net=${net:.4f} | legs={len(opp.legs)}")

    group_id = f"arb_{uuid.uuid4().hex[:8]}"
    results = []

    for leg in opp.legs:
        try:
            result = await client.place_order(
                ticker=leg["ticker"],
                side=leg["side"],
                action=leg["action"],
                count=contracts,
                price_dollars=leg["price"],
                group_id=group_id
            )
            results.append(result)
            logger.info(f"  Leg placed: {leg['ticker']} {leg['side']} x{contracts} @ {leg['price']:.4f}")
        except Exception as e:
            logger.error(f"  Leg FAILED: {leg['ticker']} — {e}")
            # TODO: cancel prior legs if using non-FOK orders
            break

    return results

# ─── Main Loop ────────────────────────────────────────────────────────────────

async def scan_and_execute(client: KalshiClient):
    """One full scan cycle."""
    logger.info("Fetching all open events...")
    events = await client.get_all_events()
    logger.info(f"Fetched {len(events)} events")

    balance = await client.get_balance()
    logger.info(f"Balance: ${balance:.2f}")

    all_opps = []

    for event in events:
        markets = event.get("markets") or []
        if not markets:
            continue

        # Type B: exhaustive sum
        opp = detect_exhaustive_sum(event, markets)
        if opp:
            all_opps.append(opp)

        # Type A: monotone containment
        monotone_opps = detect_monotone_violations(markets)
        all_opps.extend(monotone_opps)

        # Type C: binary sum (within each market)
        for m in markets:
            opp = detect_binary_sum(m)
            if opp:
                all_opps.append(opp)

    # Sort by net profit descending
    all_opps.sort(key=lambda o: o.net_profit, reverse=True)

    logger.info(f"Found {len(all_opps)} arbitrage opportunities")

    # Execute top opportunities
    for opp in all_opps:
        if opp.net_profit > 0:
            await execute_arb(client, opp, balance)
            await asyncio.sleep(0.1)  # rate limit buffer

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )
    client = KalshiClient()
    logger.info(f"Starting Kalshi Logical Arb Scanner | paper_mode={PAPER_MODE}")

    while True:
        try:
            await scan_and_execute(client)
        except Exception as e:
            logger.error(f"Scan cycle error: {e}", exc_info=True)
        await asyncio.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 10. Expected Edge & Profitability

### Historical Evidence
- Academic research on Polymarket (2024-2025) found $40M+ in combinatorial arb profits
- Top 3 wallets earned $4.2M from these strategies
- Kalshi has a similar structure but with a more traditional finance user base — potentially LESS efficient (more mispricings)

### Expected Edge Per Trade
| Arb Type          | Frequency  | Edge/Contract | Notes                              |
|-------------------|------------|---------------|------------------------------------|
| Exhaustive sum    | Low (rare) | 2–15¢         | Must have mutually_exclusive=true  |
| Monotone violation| Medium     | 0.5–5¢        | Common in fast-moving markets      |
| Binary sum        | Very rare  | 0.1–2¢        | Within single market, nearly zero  |

### Volume Considerations
- More contracts = more profit, but also more market impact
- In thin markets (< 500 open interest), buying 50 contracts may move the price
- Check orderbook depth before sizing up

### Annual Potential (rough estimate)
- If scanning 7,000 markets every 30s
- Finding 2-5 viable arbs/hour at average $5 net each
- = $10-25/hour → $87k-$219k/year (before capital constraints)
- Reality: more like $5k-$30k/year at small scale, scalable with capital

---

## 11. Environment Variables for Railway Deployment

```env
KALSHI_API_KEY_ID=your_key_id
KALSHI_PRIVATE_KEY_PEM=-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----
PAPER_MODE=true
MIN_EDGE_CENTS=0.50
MIN_NOTIONAL=5.00
POLL_INTERVAL_SEC=30
MAX_CONTRACTS=50
MAX_BALANCE_PCT=0.10
```

---

## 12. Implementation Checklist

- [ ] Authentication: RSA-PSS signing with existing private key (same as sports_bot.py)
- [ ] Market fetcher: paginate GET /events with nested markets
- [ ] Event classifier: detect mutually_exclusive, numeric strikes, binary
- [ ] Monotone checker: sort by floor_strike, compare YES asks
- [ ] Exhaustive sum checker: sum YES asks for mutually_exclusive events
- [ ] Binary sum checker: per-market YES+NO sum
- [ ] Fee calculator: taker/maker fee formulas
- [ ] Orderbook depth check: verify enough liquidity before executing
- [ ] Order executor: place FOK limit orders with cancel_order_on_pause
- [ ] Paper mode: log intended trades, no real orders
- [ ] Logging: structured output for monitoring
- [ ] Rate limiting: respect 20 req/s (Basic tier) with asyncio.sleep
- [ ] WebSocket upgrade: subscribe to ticker channel for sub-second detection

---

## 13. Key Risks & Mitigations

| Risk                        | Mitigation                                              |
|-----------------------------|---------------------------------------------------------|
| Sequential batch execution  | Use FOK; cancel all if any leg fails                    |
| Price movement between legs | Fetch fresh orderbook right before execution             |
| Market pauses               | Set cancel_order_on_pause=true on all orders            |
| Settlement rule mismatch    | Parse rules_primary to verify logical containment       |
| Fee underestimation         | Add 10% buffer to fee estimates                         |
| Thin books (slippage)       | Check orderbook depth; limit to available liquidity     |
| API downtime                | Retry with exponential backoff; don't leave open legs   |
| Capital lock-up             | Use FOK so capital is never tied up in unmatched orders |
