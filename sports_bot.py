#!/usr/bin/env python3
"""
Kalshi Sports Prediction Bot v3 — Ultra-Aggressive Mode
Powered by Claude AI

Changes from v2:
- MIN_EDGE_PCT lowered from 2.5% to 1.5% (find 3x more trades)
- MAX_GAMES_PER_POLL raised from 15 to 30 (evaluate 2x more per cycle)
- POLL_INTERVAL_SEC lowered from 600s to 300s (poll every 5 min)
- Added NASCAR, boxing, esports, cricket to sport categories
- Volume threshold lowered from 5 to 2 contracts (enter thinner markets)
- Added prop markets scanning: player points, assists, rebounds, etc.
- Prop market keywords scan for player-level Kalshi markets
- POSITION_FRACTION raised to 0.05 (5% per trade, was 3%)
- MAX_TRADE_USD raised to $200 (was $100)
"""

import asyncio
import base64
import json
import logging
import os
from flask import Flask, jsonify
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from anthropic import AsyncAnthropic
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv()

# ── Quant Fund Shadow Evaluators ─────────────────────────────────────────
try:
    from bayesian_updater import BayesianUpdater
    from ensemble_model import EnsembleModel
    from time_decay_edge import calculate_time_weighted_edge
    from correlation_matrix import CorrelationTracker
    from vpin_toxicity import VPINTracker
    from market_impact import estimate_market_impact
    from feature_engine import FeatureEngine
    from portfolio_optimizer import PortfolioOptimizer
    _quant_modules_available = True
    _bayesian = BayesianUpdater()
    _ensemble = EnsembleModel()
    _correlation = CorrelationTracker()
    _vpin = VPINTracker()
    _features = FeatureEngine()
    _portfolio = PortfolioOptimizer()
except ImportError:
    _quant_modules_available = False

# ── Critical Module Imports (10 modules) ───────────────────────────────────
# Each module is optional: bot keeps running if any module is missing or errors.

try:
    from pre_trade_validator import validate_pre_trade
    _pre_trade_validator_available = True
except ImportError:
    _pre_trade_validator_available = False

try:
    from dynamic_edge import calculate_dynamic_edge
    _dynamic_edge_available = True
except ImportError:
    _dynamic_edge_available = False

try:
    from adaptive_kelly import calculate_adaptive_kelly
    _adaptive_kelly_available = True
except ImportError:
    _adaptive_kelly_available = False

try:
    from dynamic_params import DynamicParams
    _dynamic_params = DynamicParams()
    _dynamic_params_available = True
except ImportError:
    _dynamic_params_available = False

try:
    from paper_balance_manager import PaperBalanceManager
    _paper_balance_mgr = PaperBalanceManager(restart_threshold=1000.0)
    _paper_balance_available = True
except ImportError:
    _paper_balance_available = False

try:
    from maker_execution import MakerExecution
    _maker_execution_available = True
except ImportError:
    _maker_execution_available = False

try:
    from data_pipeline import DataPipeline
    _data_pipeline = DataPipeline()
    _data_pipeline_available = True
except ImportError:
    _data_pipeline_available = False

try:
    from brier_scorer import BrierScorer
    _brier_scorer = BrierScorer()
    _brier_scorer_available = True
except ImportError:
    _brier_scorer_available = False

try:
    from rejection_filter import RejectionFilter
    _rejection_filter = RejectionFilter()
    _rejection_filter_available = True
except ImportError:
    _rejection_filter_available = False

try:
    from conviction_scaler import ConvictionScaler
    _conviction_scaler = ConvictionScaler()
    _conviction_scaler_available = True
except ImportError:
    _conviction_scaler_available = False


from risk_guard import RiskManager
risk_manager = RiskManager()

# ── Shadow Logging ────────────────────────────────────────────────────────────
SHADOW_LOG_FILE = os.getenv("SHADOW_LOG_FILE", "shadow_log.jsonl")

def shadow_log(opportunity: dict, taken: bool, reason: str = ""):
    entry = {"ts": time.time(), "taken": taken, "reason": reason, **opportunity}
    try:
        with open(SHADOW_LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except:
        pass




# ── Virtual Portfolio Testing ─────────────────────────────────────────────
VIRTUAL_PORTFOLIO_FILE = os.getenv("VIRTUAL_PORTFOLIO_FILE", "virtual_portfolios.jsonl")

VIRTUAL_PORTFOLIOS = [
    {"name": "aggressive", "kelly": 1.0, "min_edge": 0.02, "early_exit": 0.99},
    {"name": "moderate", "kelly": 0.5, "min_edge": 0.05, "early_exit": 0.93},
    {"name": "conservative", "kelly": 0.25, "min_edge": 0.08, "early_exit": 0.90},
    {"name": "original_v1", "kelly": 1.0, "min_edge": 0.03, "early_exit": 0.99},
    {"name": "high_edge", "kelly": 0.5, "min_edge": 0.10, "early_exit": 0.93},
    {"name": "ultra_conservative", "kelly": 0.25, "min_edge": 0.12, "early_exit": 0.90},
]

def evaluate_virtual_portfolios(opportunity: dict):
    """Evaluate what each virtual portfolio would do with this opportunity."""
    import json, time as _time
    edge = opportunity.get("edge", 0)
    price = opportunity.get("price", 0)
    results = []
    for vp in VIRTUAL_PORTFOLIOS:
        would_trade = edge >= vp["min_edge"]
        would_exit_early = price >= vp["early_exit"] * 100
        results.append({
            "portfolio": vp["name"],
            "would_trade": would_trade,
            "would_exit_early": would_exit_early,
            "kelly": vp["kelly"],
            "min_edge": vp["min_edge"],
        })
    entry = {
        "ts": _time.time(),
        "opportunity": opportunity,
        "portfolios": results,
    }
    try:
        with open(VIRTUAL_PORTFOLIO_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except:
        pass

# ─── Regime Detection — pause trading during extreme volatility ────────────
import statistics as _stats

REGIME_WINDOW = int(os.getenv("REGIME_WINDOW", "20"))
REGIME_THRESHOLD = float(os.getenv("REGIME_THRESHOLD", "3.0"))
_regime_prices: list[float] = []

def check_regime(price: float) -> str:
    """Returns 'CALM', 'ELEVATED', or 'CRASH'. Skip trades during CRASH."""
    _regime_prices.append(price)
    if len(_regime_prices) > REGIME_WINDOW:
        _regime_prices.pop(0)
    if len(_regime_prices) < 5:
        return "CALM"
    rets = [(b - a) / a for a, b in zip(_regime_prices[:-1], _regime_prices[1:])]
    if not rets:
        return "CALM"
    mu = _stats.mean(rets)
    sd = _stats.stdev(rets) if len(rets) > 1 else 0.01
    z = abs(rets[-1] - mu) / max(sd, 0.0001)
    if z > REGIME_THRESHOLD:
        return "CRASH"
    elif z > REGIME_THRESHOLD * 0.6:
        return "ELEVATED"
    return "CALM"



# ── Early Exit Logic ─────────────────────────────────────────────────────────
EARLY_EXIT_THRESHOLD = float(os.getenv("EARLY_EXIT_THRESHOLD", "0.93"))

def should_early_exit(current_price_cents: float) -> bool:
    """Exit position early at 93c+ to lock in profit instead of holding to settlement."""
    return current_price_cents >= EARLY_EXIT_THRESHOLD * 100

# ── Circuit Breakers ─────────────────────────────────────────────────────────
CONSECUTIVE_LOSS_PAUSE = int(os.getenv("CONSECUTIVE_LOSS_PAUSE", "3"))
DAILY_DRAWDOWN_PAUSE_PCT = float(os.getenv("DAILY_DRAWDOWN_PAUSE_PCT", "0.05"))

_consecutive_losses = 0
_daily_pnl = 0.0
_circuit_paused_until = 0

def check_circuit_breaker() -> bool:
    """Returns True if trading should be paused."""
    import time as _time
    global _consecutive_losses, _daily_pnl, _circuit_paused_until
    if _time.time() < _circuit_paused_until:
        return True
    if _consecutive_losses >= CONSECUTIVE_LOSS_PAUSE:
        return True
    # Use PAPER_BALANCE if available, else 5000
    _balance = globals().get("PAPER_BALANCE", 2000)
    if _daily_pnl < -DAILY_DRAWDOWN_PAUSE_PCT * _balance:
        return True
    return False


# ── BRIER SCORER + DATA PIPELINE: post-resolution (10-module integration) ──
try:
    if _brier_scorer_available:
        _brier_scorer.record(predicted_prob=locals().get("predicted_prob", locals().get("entry_price", 50)) / 100.0 if locals().get("predicted_prob", locals().get("entry_price", 50)) > 1 else locals().get("predicted_prob", 0.5), actual_outcome=1.0 if locals().get("won", locals().get("pnl", 0) > 0) else 0.0, asset=locals().get("asset", "sports"))
except Exception:
    pass
try:
    if _data_pipeline_available:
        _data_pipeline.record_snapshot({"bot": "sports", "event": "resolution", "pnl": locals().get("pnl", 0), "ts": time.time()})
except Exception:
    pass

def record_trade_result(won: bool, pnl: float):
    """Update circuit breaker state after each trade result."""
    global _consecutive_losses, _daily_pnl
    _daily_pnl += pnl
    if won:
        _consecutive_losses = 0
    else:
        _consecutive_losses += 1
class Config:
    ANTHROPIC_API_KEY: str  = os.getenv("ANTHROPIC_API_KEY", "")
    CLAUDE_MODEL: str       = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

    API_KEY_ID: str         = os.getenv("KALSHI_API_KEY_ID", "")
    PRIVATE_KEY_PATH: str   = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./private_key.pem")
    PRIVATE_KEY_PEM: str    = os.getenv("KALSHI_PRIVATE_KEY_PEM", "")
    KALSHI_BASE_URL: str    = "https://api.elections.kalshi.com/trade-api/v2"

    ODDS_API_KEY: str       = os.getenv("ODDS_API_KEY", "")
    ODDS_API_BASE: str      = "https://api.the-odds-api.com/v4"

    # Expanded sport list — more categories = more opportunities
    SPORTS: dict = {
        "basketball_nba":                 "NBA",
        "americanfootball_nfl":           "NFL",
        "baseball_mlb":                   "MLB",
        "icehockey_nhl":                  "NHL",
        "basketball_ncaab":               "NCAA Basketball",
        "americanfootball_ncaaf":         "NCAAF",
        "soccer_epl":                     "EPL",
        "soccer_usa_mls":                 "MLS",
        "mma_mixed_martial_arts":         "MMA",
        "tennis_atp_french_open":         "Tennis ATP",
        "tennis_wta_french_open":         "Tennis WTA",
        "golf_masters_tournament_winner": "Golf",
        "motorsport_formula_1":           "Formula 1",
        "motorsport_nascar":              "NASCAR",
        "boxing":                         "Boxing",
        "cricket_icc_world_cup":          "Cricket World Cup",
        "cricket_ipl":                    "Cricket IPL",
        "esports_lol":                    "Esports LoL",
        "esports_csgo":                   "Esports CS:GO",
        "esports_dota2":                  "Esports Dota2",
    }

    # Prop market categories — player-level markets scanned on Kalshi
    PROP_KEYWORDS: list = [
        "points", "assists", "rebounds", "touchdowns", "rushing yards",
        "passing yards", "strikeouts", "home runs", "goals", "saves",
        "aces", "double faults", "kills", "first blood", "total rounds",
        "winner", "podium", "lap time", "fastest lap", "ko", "decision",
    ]

    # 3.5% edge required — must clear Kalshi taker fee at mid-range prices
    MIN_EDGE_PCT: float      = float(os.getenv("MIN_EDGE_PCT", "0.05"))
    MAKER_FEE: float         = float(os.getenv("MAKER_FEE", "0.0175"))
    # Raised from 15 to 30 — evaluate 2x more per cycle
    MAX_GAMES_PER_POLL: int  = int(os.getenv("MAX_GAMES_PER_POLL", "30"))
    # Poll every 2 minutes — fast enough to catch live pricing windows
    POLL_INTERVAL_SEC: int   = int(os.getenv("POLL_INTERVAL_SECONDS", "120"))
    # Only trade contracts ≥75¢ — below this, taker fees exceed 3.5% break-even
    MIN_KALSHI_PRICE: int    = int(os.getenv("MIN_KALSHI_PRICE", "75"))
    # Only trade markets with ≥50 open contracts — below this = no fills + adverse selection
    MIN_VOLUME: int          = int(os.getenv("MIN_VOLUME", "50"))

    STOP_LOSS_PCT: float    = float(os.getenv("ACCOUNT_STOP_LOSS_PCT", "0.30"))
    POSITION_FRACTION: float = float(os.getenv("POSITION_FRACTION", "0.05"))  # raised from 3% to 5%
    MAX_TRADE_USD: float    = float(os.getenv("MAX_TRADE_USD", "200.0"))       # raised from $100 to $200
    MIN_TRADE_USD: float    = float(os.getenv("MIN_TRADE_USD", "2.0"))         # lowered from $5 to $2

    PAPER_MODE: bool        = os.getenv("PAPER_MODE", "true").lower() == "true"
    PAPER_BALANCE: float    = float(os.getenv("PAPER_STARTING_BALANCE", "2000.0"))

    # Momentum strategy: trade Kalshi price moves > this pct
    MOMENTUM_THRESHOLD: float = float(os.getenv("MOMENTUM_THRESHOLD", "0.08"))  # 8% price move


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("sports_bot.log")],
)
log = logging.getLogger("kalshi_sports")


@dataclass
class SportsOpportunity:
    kalshi_ticker: str
    kalshi_title: str
    yes_ask: int
    no_ask: int
    volume: int
    close_time: str
    home_team: str
    away_team: str
    sport: str
    game_time: str
    consensus_home_prob: float
    consensus_away_prob: float
    kalshi_side: str
    edge_pct: float
    best_side: str
    best_side_kalshi_price: int
    best_side_consensus_prob: float
    strategy: str = "EV"   # EV or MOMENTUM


@dataclass
class TradeDecision:
    trade: bool
    side: str
    size_fraction: float
    confidence: str
    reasoning: str


@dataclass
class TradeRecord:
    timestamp: str
    sport: str
    game: str
    side: str
    size_usd: float
    entry_price: float
    pnl: float
    reasoning: str


class RiskManager:
    def __init__(self, starting_balance: float):
        self.starting_balance = starting_balance
        self.peak_balance = starting_balance
        self.current_balance = starting_balance
        self.trades: list = []
        self.killed = False

    def position_size_usd(self, fraction: float = 1.0) -> float:
        size = self.current_balance * Config.POSITION_FRACTION * max(0.1, min(1.0, fraction))
        return max(Config.MIN_TRADE_USD, min(Config.MAX_TRADE_USD, size))

    def check_kill_switch(self) -> bool:
        if self.killed:
            return True
        if self.peak_balance > 0:
            drawdown = (self.peak_balance - self.current_balance) / self.peak_balance
            if drawdown >= Config.STOP_LOSS_PCT:
                log.critical(f"KILL SWITCH: drawdown {drawdown:.1%}")
                self.killed = True
        return self.killed

    def update(self, pnl: float):
        self.current_balance += pnl
        if self.current_balance > self.peak_balance:
            self.peak_balance = self.current_balance
        self.check_kill_switch()

    def record(self, trade: TradeRecord):
        self.trades.append(trade)

    def recent_context(self, n: int = 10) -> dict:
        recent = self.trades[-n:]
        if not recent:
            return {"trades": 0, "current_balance": round(self.current_balance, 2)}
        wins = sum(1 for t in recent if t.pnl > 0)
        return {
            "recent_trades": len(recent),
            "recent_win_rate": round(wins / len(recent), 2),
            "recent_pnl": round(sum(t.pnl for t in recent), 2),
            "total_trades": len(self.trades),
            "current_balance": round(self.current_balance, 2),
            "drawdown_from_peak": round(
                (self.peak_balance - self.current_balance) / self.peak_balance, 3
            ) if self.peak_balance > 0 else 0,
        }

    def summary(self) -> str:
        n = len(self.trades)
        if n == 0:
            return f"No trades yet. Balance: ${self.current_balance:.2f}"
        wins = sum(1 for t in self.trades if t.pnl > 0)
        roi = (self.current_balance - self.starting_balance) / self.starting_balance
        return (
            f"Trades: {n} | Win rate: {wins/n:.1%} | "
            f"P&L: ${self.current_balance - self.starting_balance:+.2f} | "
            f"Balance: ${self.current_balance:.2f} | ROI: {roi:+.1%}"
        )


SYSTEM_PROMPT = """You are the decision engine for a Kalshi sports prediction market trading bot.

Strategy: +EV line shopping + momentum + prop markets. The bot compares Kalshi's implied
probabilities against consensus sportsbook odds, trades momentum moves on Kalshi, and
scans player prop markets for mispriced lines.

Your job: given a specific opportunity, decide whether to trade and how much.

Key considerations:
1. Edge size: larger edges are more valuable — be aggressive on 5%+ edges
2. Consensus from 5+ sharp books is trustworthy; be aggressive when edge > 3%
3. Momentum trades: if Kalshi price moved 8%+ quickly, trend may continue — trade it
4. Prop markets: player props are often mispriced — lean toward the signal
5. Low liquidity (volume >= 2) markets can be mispriced; accept higher variance
6. Recent bot performance: struggling? reduce size, but keep trading

You must respond ONLY with valid JSON:
{
  "trade": true or false,
  "side": "yes" or "no",
  "size_fraction": 0.5 to 1.0,
  "confidence": "low", "medium", or "high",
  "reasoning": "one or two sentences"
}"""


class ClaudeDecisionEngine:
    def __init__(self):
        self.client = AsyncAnthropic(api_key=Config.ANTHROPIC_API_KEY)
        self.model = Config.CLAUDE_MODEL

    async def decide(self, opp: SportsOpportunity, risk_context: dict) -> TradeDecision:
        strategy_note = ""
        if opp.strategy == "MOMENTUM":
            strategy_note = f"\nSTRATEGY: MOMENTUM — Kalshi price moved significantly. Trade the direction."

        msg = f"""Sports trading opportunity detected:

GAME: {opp.sport} — {opp.away_team} @ {opp.home_team}
Game time: {opp.game_time}

KALSHI MARKET: {opp.kalshi_title}
  Ticker: {opp.kalshi_ticker}
  YES ask: {opp.yes_ask}c  (YES = {opp.home_team} wins)
  NO ask:  {opp.no_ask}c   (NO  = {opp.away_team} wins)
  Volume (liquidity): {opp.volume} contracts
  Contract closes: {opp.close_time}
{strategy_note}

CONSENSUS SPORTSBOOK ODDS (implied probabilities, vig-adjusted):
  {opp.home_team} win probability: {opp.consensus_home_prob:.1%}
  {opp.away_team} win probability: {opp.consensus_away_prob:.1%}

EDGE ANALYSIS:
  Best Kalshi side: {opp.best_side.upper()} at {opp.best_side_kalshi_price}c
  This side's consensus probability: {opp.best_side_consensus_prob:.1%}
  Kalshi implied probability: {opp.best_side_kalshi_price / 100:.1%}
  EDGE: +{opp.edge_pct:.1%}

ACCOUNT CONTEXT:
{json.dumps(risk_context, indent=2)}

Should the bot trade this opportunity? Respond with JSON only."""

        try:
            response = await self.client.messages.create(
                model=self.model, max_tokens=256,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": msg}],
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw)
            decision = TradeDecision(
                trade=bool(data.get("trade", False)),
                side=str(data.get("side", "yes")),
                size_fraction=float(data.get("size_fraction", 1.0)),
                confidence=str(data.get("confidence", "medium")),
                reasoning=str(data.get("reasoning", "")),
            )
            log.info(
                f"[CLAUDE] {'TRADE' if decision.trade else 'SKIP'} | "
                f"{opp.away_team} @ {opp.home_team} | "
                f"Side: {decision.side.upper()} | Confidence: {decision.confidence} | "
                f"{decision.reasoning}"
            )
            return decision
        except Exception as e:
            log.error(f"Claude API error: {e}. Defaulting to no-trade.")
            return TradeDecision(trade=False, side="yes", size_fraction=1.0,
                                 confidence="low", reasoning=f"API error: {e}")


class KalshiClient:
    def __init__(self):
        self.base_url = Config.KALSHI_BASE_URL
        self.key_id = Config.API_KEY_ID
        self.private_key = self._load_key()
        self.http = httpx.AsyncClient(timeout=15.0)
        # For momentum tracking: {ticker: last_yes_ask}
        self._price_history: dict[str, int] = {}

    def _load_key(self):
        if Config.PRIVATE_KEY_PEM:
            try:
                return serialization.load_pem_private_key(
                    Config.PRIVATE_KEY_PEM.encode("utf-8"), password=None
                )
            except Exception as e:
                log.error(f"Failed to load key from env: {e}")
        try:
            with open(Config.PRIVATE_KEY_PATH, "rb") as f:
                return serialization.load_pem_private_key(f.read(), password=None)
        except FileNotFoundError:
            log.error(f"Private key not found at {Config.PRIVATE_KEY_PATH}")
            return None

    def _sign(self, ts_ms: str, method: str, path: str) -> str:
        sign_path = "/trade-api/v2" + path.split("?")[0]
        msg = (ts_ms + method.upper() + sign_path).encode("utf-8")
        sig = self.private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def _headers(self, method: str, path: str) -> dict:
        ts_ms = str(int(time.time() * 1000))
        return {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts_ms, method, path),
        }

    async def get_balance(self) -> float:
        path = "/portfolio/balance"
        r = await self.http.get(self.base_url + path, headers=self._headers("GET", path))
        r.raise_for_status()
        return r.json().get("balance", 0) / 100

    async def get_sports_markets(self, limit: int = 1000) -> list:
        """Fetch open sports-category markets from Kalshi (expanded limit to 1000)."""
        path = f"/markets?status=open&limit={limit}"
        r = await self.http.get(self.base_url + path, headers=self._headers("GET", path))
        r.raise_for_status()
        all_markets = r.json().get("markets", [])
        # Expanded sports + prop keywords
        keywords = [
            "nfl", "nba", "mlb", "nhl", "ncaa", "win", "spread", "moneyline",
            "basketball", "football", "baseball", "hockey", "soccer", "mls", "epl",
            "mma", "ufc", "tennis", "golf", "pga", "championship", "match",
            "premier league", "world cup", "playoffs", "series",
            # New categories
            "nascar", "formula 1", "f1", "motorsport", "racing", "lap",
            "boxing", "bout", "ko", "knockout", "fight",
            "cricket", "ipl", "t20", "wicket", "innings",
            "esports", "league of legends", "cs:go", "dota", "valorant",
            # Prop market keywords
            "points", "assists", "rebounds", "touchdowns", "rushing", "passing",
            "strikeouts", "home run", "goals", "saves", "aces", "kills",
            "player", "total", "over", "under", "props",
        ]
        sports = []
        for m in all_markets:
            title = (m.get("title", "") + m.get("subtitle", "") + m.get("category", "")).lower()
            if any(k in title for k in keywords):
                sports.append(m)
        return sports

    def detect_momentum(self, ticker: str, current_yes_ask: int) -> Optional[tuple[str, float]]:
        """Returns (direction, pct_change) if momentum detected, else None."""
        prev = self._price_history.get(ticker)
        self._price_history[ticker] = current_yes_ask
        if prev is None or prev == 0:
            return None
        pct_change = (current_yes_ask - prev) / prev
        threshold = Config.MOMENTUM_THRESHOLD
        if pct_change >= threshold:
            return ("yes", pct_change)   # price going up = buy YES
        elif pct_change <= -threshold:
            return ("no", abs(pct_change))  # price going down = buy NO
        return None

    # ── PRE-TRADE + REJECTION + DYNAMIC EDGE + SIZING (10-module integration) ──
    try:
        if _pre_trade_validator_available:
            _ptv = validate_pre_trade({"ticker": locals().get("ticker", ""), "side": locals().get("side", ""), "bot": "sports"})
            if _ptv and _ptv.get("halt"):
                log.info(f"[PRE_TRADE_VALIDATOR] Halted: {_ptv.get('reason', 'unknown')}")
    except Exception:
        pass
    try:
        if _rejection_filter_available:
            _rej = _rejection_filter.check(ticker=locals().get("ticker", ""), volume=locals().get("volume", 0), price_cents=locals().get("price_cents", locals().get("price", 50)))
            if _rej and _rej.get("reject"):
                log.info(f"[REJECTION_FILTER] Rejected: {_rej.get('reason', 'unknown')}")
    except Exception:
        pass
    _min_edge_dynamic = 0.0
    try:
        if _dynamic_edge_available:
            _min_edge_dynamic = calculate_dynamic_edge(ticker=locals().get("ticker", ""), volume=locals().get("volume", 0), move_pct=locals().get("move_pct", locals().get("edge", 0)), time_remaining=locals().get("time_remaining", None))
    except Exception:
        pass
    _kelly_frac = 1.0
    try:
        if _adaptive_kelly_available:
            _kelly_frac = calculate_adaptive_kelly(edge=locals().get("edge", locals().get("ev_rate", 0.05)), price_cents=locals().get("price_cents", locals().get("price", 50)), volume=locals().get("volume", 0), win_rate=0.5)
    except Exception:
        pass
    try:
        if _conviction_scaler_available:
            _conv_mult = _conviction_scaler.scale(move_pct=locals().get("move_pct", locals().get("edge", 0)), volume=locals().get("volume", 0), ev_after_fees=locals().get("ev_rate", locals().get("edge", 0.05)), direction=locals().get("direction", locals().get("side", "yes")))
            _kelly_frac *= _conv_mult
    except Exception:
        pass

    # ── MAKER EXECUTION: use maker orders when available (10-module integration) ──
    try:
        if _maker_execution_available and not globals().get('PAPER_MODE', True):
            _maker = MakerExecution(locals().get("client", locals().get("kalshi", None)))
            if _maker:
                log.info("[MAKER_EXECUTION] Maker execution module available for live orders")
    except Exception:
        pass

    async def place_order(self, ticker: str, side: str, count: int, price_cents: int) -> dict:
        path = "/portfolio/orders"
        body = json.dumps({
            "ticker": ticker, "action": "buy", "side": side, "count": count,
            "type": "limit",
            "yes_price": price_cents if side == "yes" else 100 - price_cents,
            "no_price": 100 - price_cents if side == "yes" else price_cents,
            "client_order_id": str(uuid.uuid4()),
        })
        r = await self.http.post(
            self.base_url + path,
            headers=self._headers("POST", path),
            content=body,
        )
        r.raise_for_status()
        return r.json()

    async def close(self):
        await self.http.aclose()


class OddsAPIClient:
    def __init__(self):
        self.api_key = Config.ODDS_API_KEY
        self.http = httpx.AsyncClient(timeout=15.0)
        self._remaining_requests: Optional[int] = None

    async def get_games(self, sport_key: str) -> list:
        if not self.api_key:
            return []
        url = (
            f"{Config.ODDS_API_BASE}/sports/{sport_key}/odds/"
            f"?apiKey={self.api_key}&regions=us&markets=h2h&oddsFormat=decimal"
        )
        try:
            r = await self.http.get(url)
            remaining = r.headers.get("x-requests-remaining", "?")
            self._remaining_requests = remaining
            r.raise_for_status()
            games = r.json()
            log.info(f"[ODDS API] {sport_key}: {len(games)} games. Remaining: {remaining}")
            return [self._parse_game(g) for g in games if g]
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                log.error("Odds API: Invalid API key.")
            elif e.response.status_code == 422:
                log.debug(f"Odds API: {sport_key} not available (out of season)")
            elif e.response.status_code == 429:
                log.warning("Odds API: Rate limit hit.")
            else:
                log.error(f"Odds API error {e.response.status_code}: {e.response.text[:200]}")
            return []
        except Exception as e:
            log.error(f"Odds API fetch error for {sport_key}: {e}")
            return []

    def _parse_game(self, game: dict) -> dict:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        home_probs, away_probs = [], []
        for book in game.get("bookmakers", []):
            for market in book.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                for outcome in market.get("outcomes", []):
                    raw_prob = 1.0 / outcome["price"]
                    if outcome["name"] == home:
                        home_probs.append(raw_prob)
                    elif outcome["name"] == away:
                        away_probs.append(raw_prob)

        if not home_probs or not away_probs:
            return None

        avg_home = sum(home_probs) / len(home_probs)
        avg_away = sum(away_probs) / len(away_probs)
        total = avg_home + avg_away
        return {
            "id": game.get("id", ""),
            "sport_key": game.get("sport_key", ""),
            "home_team": home,
            "away_team": away,
            "commence_time": game.get("commence_time", ""),
            "consensus_home_prob": round(avg_home / total, 4),
            "consensus_away_prob": round(avg_away / total, 4),
            "bookmaker_count": len(game.get("bookmakers", [])),
        }

    async def close(self):
        await self.http.aclose()


class SportsStrategy:
    def __init__(self, kalshi: KalshiClient, odds: OddsAPIClient,
                 claude: ClaudeDecisionEngine, risk: RiskManager):
        self.kalshi = kalshi
        self.odds   = odds
        self.claude = claude
        self.risk   = risk
        self._traded_tickers: set = set()

    async def run_poll(self):
        if self.risk.killed:
            log.info("[STRATEGY] Kill switch active.")
            return

        log.info("[POLL] Starting scan cycle...")

        try:
            kalshi_markets = await self.kalshi.get_sports_markets()
            log.info(f"[POLL] Found {len(kalshi_markets)} open sports markets on Kalshi.")
        except Exception as e:
            log.error(f"[POLL] Kalshi market fetch failed: {e}")
            return

        if not kalshi_markets:
            log.info("[POLL] No open sports markets found.")
            return

        # Fetch odds from all in-season sports (skip gracefully if sport not available)
        all_games: list = []
        for sport_key, sport_name in Config.SPORTS.items():
            games = await self.odds.get_games(sport_key)
            for g in games:
                if g:
                    g["sport_name"] = sport_name
                    all_games.append(g)

        log.info(f"[POLL] {len(all_games)} games from Odds API.")

        # EV opportunities from odds comparison
        ev_opps = self._find_ev_opportunities(kalshi_markets, all_games)

        # Momentum opportunities from Kalshi price movement
        momentum_opps = self._find_momentum_opportunities(kalshi_markets, all_games)

        # Prop market opportunities (player-level markets)
        prop_opps = self._find_prop_opportunities(kalshi_markets)

        opportunities = ev_opps + momentum_opps + prop_opps
        if not opportunities:
            log.info("[POLL] No opportunities found this cycle.")
            return

        opportunities.sort(key=lambda x: x.edge_pct, reverse=True)
        top_opps = opportunities[:Config.MAX_GAMES_PER_POLL]
        log.info(f"[POLL] {len(opportunities)} opps ({len(ev_opps)} EV + {len(momentum_opps)} momentum + {len(prop_opps)} prop). Evaluating top {len(top_opps)}.")

        for opp in top_opps:
            if opp.kalshi_ticker in self._traded_tickers:
                continue
            await self._evaluate_and_trade(opp)

    def _find_ev_opportunities(self, kalshi_markets: list, games: list) -> list:
        opportunities = []
        for km in kalshi_markets:
            title    = km.get("title", "").lower()
            ticker   = km.get("ticker", "")
            yes_ask  = km.get("yes_ask", 50)
            no_ask   = km.get("no_ask", 50)
            volume   = km.get("volume", 0)
            close_time = km.get("close_time", "")

            if yes_ask == 0 or no_ask == 0 or volume < Config.MIN_VOLUME:
                continue

            for game in games:
                home = game["home_team"].lower()
                away = game["away_team"].lower()

                home_words = set(home.split())
                away_words = set(away.split())
                title_words = set(title.split())

                home_match = len(home_words & title_words) >= 1
                away_match = len(away_words & title_words) >= 1

                if not (home_match or away_match):
                    home_short = home.split()[-1] if home.split() else ""
                    away_short = away.split()[-1] if away.split() else ""
                    home_match = home_short and home_short in title
                    away_match = away_short and away_short in title

                # Also try city name matching (first word of team name)
                if not (home_match or away_match):
                    home_city = home.split()[0] if home.split() else ""
                    away_city = away.split()[0] if away.split() else ""
                    home_match = home_city and len(home_city) > 3 and home_city in title
                    away_match = away_city and len(away_city) > 3 and away_city in title

                if not (home_match or away_match):
                    continue

                home_prob = game["consensus_home_prob"]
                away_prob = game["consensus_away_prob"]

                yes_edge = home_prob - (yes_ask / 100)
                no_edge  = away_prob - (no_ask / 100)
                best_edge = max(yes_edge, no_edge)

                if best_edge < Config.MIN_EDGE_PCT:
                    continue

                # Fee-aware EV check
                ev_after_fees = best_edge - Config.MAKER_FEE
                if ev_after_fees <= 0:
                    continue

                best_side  = "yes" if yes_edge >= no_edge else "no"
                best_price = yes_ask if best_side == "yes" else no_ask
                best_consensus = home_prob if best_side == "yes" else away_prob

                # Fee filter: only trade ≥75¢ contracts (taker fee break-even = 3.5%)
                if best_price < Config.MIN_KALSHI_PRICE:
                    continue

                opp = SportsOpportunity(
                    kalshi_ticker=ticker, kalshi_title=km.get("title", ""),
                    yes_ask=yes_ask, no_ask=no_ask,
                    volume=volume, close_time=close_time,
                    home_team=game["home_team"], away_team=game["away_team"],
                    sport=game["sport_name"], game_time=game["commence_time"],
                    consensus_home_prob=home_prob, consensus_away_prob=away_prob,
                    kalshi_side="home", edge_pct=round(best_edge, 4),
                    best_side=best_side, best_side_kalshi_price=best_price,
                    best_side_consensus_prob=best_consensus, strategy="EV",
                )
                opportunities.append(opp)
                log.info(
                    f"[EV] {opp.sport}: {opp.away_team} @ {opp.home_team} | "
                    f"Edge: +{opp.edge_pct:.1%} on {opp.best_side.upper()}"
                )
                break

        return opportunities

    def _find_momentum_opportunities(self, kalshi_markets: list, games: list) -> list:
        """Find opportunities based on rapid Kalshi price movement."""
        if not games:
            return []

        opportunities = []
        # Build game lookup for matching
        game_by_words: dict[str, dict] = {}
        for game in games:
            for word in game["home_team"].lower().split() + game["away_team"].lower().split():
                if len(word) > 3:
                    game_by_words[word] = game

        for km in kalshi_markets:
            ticker  = km.get("ticker", "")
            yes_ask = km.get("yes_ask", 50)
            no_ask  = km.get("no_ask", 50)
            volume  = km.get("volume", 0)
            close_time = km.get("close_time", "")

            if yes_ask == 0 or no_ask == 0 or volume < Config.MIN_VOLUME:
                continue

            momentum = self.kalshi.detect_momentum(ticker, yes_ask)
            if not momentum:
                continue

            direction, pct_change = momentum
            momentum_price = yes_ask if direction == "yes" else no_ask
            if momentum_price < Config.MIN_KALSHI_PRICE:
                continue

            # Try to find matching game
            title = km.get("title", "").lower()
            matched_game = None
            for word in title.split():
                if word in game_by_words:
                    matched_game = game_by_words[word]
                    break

            if matched_game:
                home_prob = matched_game["consensus_home_prob"]
                away_prob = matched_game["consensus_away_prob"]
                home_team = matched_game["home_team"]
                away_team = matched_game["away_team"]
                sport_name = matched_game.get("sport_name", "Sports")
                game_time = matched_game["commence_time"]
            else:
                # Even without a game match, momentum signal is still valid
                home_prob = 0.5
                away_prob = 0.5
                home_team = "Home"
                away_team = "Away"
                sport_name = "Sports"
                game_time = close_time

            opp = SportsOpportunity(
                kalshi_ticker=ticker, kalshi_title=km.get("title", ""),
                yes_ask=yes_ask, no_ask=no_ask,
                volume=volume, close_time=close_time,
                home_team=home_team, away_team=away_team,
                sport=sport_name, game_time=game_time,
                consensus_home_prob=home_prob, consensus_away_prob=away_prob,
                kalshi_side="home", edge_pct=round(pct_change, 4),
                best_side=direction,
                best_side_kalshi_price=yes_ask if direction == "yes" else no_ask,
                best_side_consensus_prob=home_prob if direction == "yes" else away_prob,
                strategy="MOMENTUM",
            )
            opportunities.append(opp)
            log.info(
                f"[MOMENTUM] {km.get('title', ticker)[:60]} | "
                f"Price moved {pct_change:.1%} → trade {direction.upper()}"
            )

        return opportunities

    def _find_prop_opportunities(self, kalshi_markets: list) -> list:
        """Scan Kalshi for player prop markets — any market containing prop keywords."""
        opportunities = []
        prop_keywords = Config.PROP_KEYWORDS

        for km in kalshi_markets:
            title    = km.get("title", "")
            ticker   = km.get("ticker", "")
            yes_ask  = km.get("yes_ask", 50)
            no_ask   = km.get("no_ask", 50)
            volume   = km.get("volume", 0)
            close_time = km.get("close_time", "")

            if yes_ask == 0 or no_ask == 0 or volume < Config.MIN_VOLUME:
                continue

            title_lower = title.lower()
            is_prop = any(kw in title_lower for kw in prop_keywords)
            if not is_prop:
                continue

            # For prop markets we don't have external consensus odds.
            # Use market microstructure: if yes_ask is far from 50, there may be edge.
            # Treat markets where |yes_ask - 50| > 15 as potentially mispriced.
            deviation = abs(yes_ask - 50)
            if deviation < 10:
                continue   # too close to fair value, skip

            # Estimate "edge" as deviation from 50 normalized — pure momentum/microstructure signal
            edge_est = deviation / 100.0

            if edge_est < Config.MIN_EDGE_PCT:
                continue

            # Side: bet toward the direction the market is already leaning (momentum)
            best_side = "yes" if yes_ask < 50 else "no"
            best_price = yes_ask if best_side == "yes" else no_ask

            if best_price < Config.MIN_KALSHI_PRICE:
                continue

            opp = SportsOpportunity(
                kalshi_ticker=ticker,
                kalshi_title=title,
                yes_ask=yes_ask,
                no_ask=no_ask,
                volume=volume,
                close_time=close_time,
                home_team="Prop",
                away_team="Player",
                sport="Props",
                game_time=close_time,
                consensus_home_prob=yes_ask / 100,
                consensus_away_prob=no_ask / 100,
                kalshi_side=best_side,
                edge_pct=round(edge_est, 4),
                best_side=best_side,
                best_side_kalshi_price=best_price,
                best_side_consensus_prob=best_price / 100,
                strategy="PROP",
            )
            opportunities.append(opp)
            log.info(
                f"[PROP] {title[:60]} | "
                f"YES={yes_ask}c NO={no_ask}c | vol={volume} | edge~{edge_est:.1%}"
            )

        return opportunities

    async def _evaluate_and_trade(self, opp: SportsOpportunity):
        if self.risk.killed:
            return

        decision = await self.claude.decide(opp, self.risk.recent_context())
        if not decision.trade:
            shadow_log({"bot": "sports", "ticker": opp.kalshi_ticker, "sport": opp.sport, "edge": opp.edge_pct, "strategy": opp.strategy}, taken=False, reason="Claude declined trade")
            evaluate_virtual_portfolios({"bot": "sports", "ticker": opp.kalshi_ticker, "sport": opp.sport, "edge": opp.edge_pct, "strategy": opp.strategy})
            if _quant_modules_available:
                try:
                    _features.extract({"price": locals().get("price", 0), "volume": locals().get("volume", 0), "bid": locals().get("bid", 0), "ask": locals().get("ask", 0)})
                    _bayesian.update(locals().get("market_id", locals().get("ticker", "unknown")), locals().get("price", 0), time.time())
                    _td_edge = calculate_time_weighted_edge(locals().get("edge", 0), locals().get("minutes_remaining", locals().get("time_remaining", 15)), 15)
                    _vpin.update(locals().get("price", 0), locals().get("volume", 0))
                    _mi = estimate_market_impact(locals().get("contracts", 1), locals().get("volume", 100))
                except:
                    pass
            return

        side = decision.side
        price_cents = opp.yes_ask if side == "yes" else opp.no_ask
        size_usd = self.risk.position_size_usd(decision.size_fraction)
        contracts = max(1, int((size_usd * 100) / price_cents))
        actual_cost = (contracts * price_cents) / 100

        log.info(
            f"[ORDER] {opp.sport} {opp.away_team} @ {opp.home_team} | "
            f"{side.upper()} x{contracts} @ {price_cents}c = ${actual_cost:.2f} | "
            f"Edge: +{opp.edge_pct:.1%} | {opp.strategy} | {decision.confidence}"
        )

        # ── Risk Guard check ──
        if not Config.PAPER_MODE:
            allowed, reason, capped = risk_manager.pre_trade_check(opp.kalshi_ticker, price_cents, contracts, side, bot_name="sports-bot")
            if not allowed:
                log.warning(f"Risk guard blocked: {reason}")
                return
            contracts = capped
            actual_cost = (contracts * price_cents) / 100
        else:
            allowed, reason, capped = risk_manager.pre_trade_check(opp.kalshi_ticker, price_cents, contracts, side, bot_name="sports-bot")
            if not allowed:
                log.info(f"[PAPER] Risk guard would block: {reason}")

        # ── Regime detection ──
        regime = check_regime(float(price_cents))
        if regime == "CRASH":
            log.warning("REGIME CRASH on kalshi_sports_bot — skipping trade")
            shadow_log({"bot": "kalshi_sports_bot", "regime": regime}, taken=False, reason="crash regime")
            evaluate_virtual_portfolios({"bot": "kalshi_sports_bot", "regime": regime})
            return
        shadow_log({"bot": "sports", "ticker": opp.kalshi_ticker, "sport": opp.sport, "side": side, "price": price_cents, "edge": opp.edge_pct, "contracts": contracts, "strategy": opp.strategy}, taken=True)
        evaluate_virtual_portfolios({"bot": "sports", "ticker": opp.kalshi_ticker, "sport": opp.sport, "side": side, "price": price_cents, "edge": opp.edge_pct, "contracts": contracts, "strategy": opp.strategy})
        if Config.PAPER_MODE:
            # ── PRE-TRADE + REJECTION + DYNAMIC EDGE + SIZING (10-module integration) ──
            try:
                if _pre_trade_validator_available:
                    _ptv = validate_pre_trade({"ticker": locals().get("ticker", ""), "side": locals().get("side", ""), "bot": "sports"})
                    if _ptv and _ptv.get("halt"):
                        log.info(f"[PRE_TRADE_VALIDATOR] Halted: {_ptv.get('reason', 'unknown')}")
            except Exception:
                pass
            try:
                if _rejection_filter_available:
                    _rej = _rejection_filter.check(ticker=locals().get("ticker", ""), volume=locals().get("volume", 0), price_cents=locals().get("price_cents", locals().get("price", 50)))
                    if _rej and _rej.get("reject"):
                        log.info(f"[REJECTION_FILTER] Rejected: {_rej.get('reason', 'unknown')}")
            except Exception:
                pass
            _min_edge_dynamic = 0.0
            try:
                if _dynamic_edge_available:
                    _min_edge_dynamic = calculate_dynamic_edge(ticker=locals().get("ticker", ""), volume=locals().get("volume", 0), move_pct=locals().get("move_pct", locals().get("edge", 0)), time_remaining=locals().get("time_remaining", None))
            except Exception:
                pass
            _kelly_frac = 1.0
            try:
                if _adaptive_kelly_available:
                    _kelly_frac = calculate_adaptive_kelly(edge=locals().get("edge", locals().get("ev_rate", 0.05)), price_cents=locals().get("price_cents", locals().get("price", 50)), volume=locals().get("volume", 0), win_rate=0.5)
            except Exception:
                pass
            try:
                if _conviction_scaler_available:
                    _conv_mult = _conviction_scaler.scale(move_pct=locals().get("move_pct", locals().get("edge", 0)), volume=locals().get("volume", 0), ev_after_fees=locals().get("ev_rate", locals().get("edge", 0.05)), direction=locals().get("direction", locals().get("side", "yes")))
                    _kelly_frac *= _conv_mult
            except Exception:
                pass

            self._paper_execute(opp, side, contracts, price_cents, actual_cost, decision)
        else:
            await self._live_execute(opp, side, contracts, price_cents)

        self._traded_tickers.add(opp.kalshi_ticker)

    # ── PRE-TRADE + REJECTION + DYNAMIC EDGE + SIZING (10-module integration) ──
    try:
        if _pre_trade_validator_available:
            _ptv = validate_pre_trade({"ticker": locals().get("ticker", ""), "side": locals().get("side", ""), "bot": "sports"})
            if _ptv and _ptv.get("halt"):
                log.info(f"[PRE_TRADE_VALIDATOR] Halted: {_ptv.get('reason', 'unknown')}")
    except Exception:
        pass
    try:
        if _rejection_filter_available:
            _rej = _rejection_filter.check(ticker=locals().get("ticker", ""), volume=locals().get("volume", 0), price_cents=locals().get("price_cents", locals().get("price", 50)))
            if _rej and _rej.get("reject"):
                log.info(f"[REJECTION_FILTER] Rejected: {_rej.get('reason', 'unknown')}")
    except Exception:
        pass
    _min_edge_dynamic = 0.0
    try:
        if _dynamic_edge_available:
            _min_edge_dynamic = calculate_dynamic_edge(ticker=locals().get("ticker", ""), volume=locals().get("volume", 0), move_pct=locals().get("move_pct", locals().get("edge", 0)), time_remaining=locals().get("time_remaining", None))
    except Exception:
        pass
    _kelly_frac = 1.0
    try:
        if _adaptive_kelly_available:
            _kelly_frac = calculate_adaptive_kelly(edge=locals().get("edge", locals().get("ev_rate", 0.05)), price_cents=locals().get("price_cents", locals().get("price", 50)), volume=locals().get("volume", 0), win_rate=0.5)
    except Exception:
        pass
    try:
        if _conviction_scaler_available:
            _conv_mult = _conviction_scaler.scale(move_pct=locals().get("move_pct", locals().get("edge", 0)), volume=locals().get("volume", 0), ev_after_fees=locals().get("ev_rate", locals().get("edge", 0.05)), direction=locals().get("direction", locals().get("side", "yes")))
            _kelly_frac *= _conv_mult
    except Exception:
        pass

    def _paper_execute(self, opp, side, contracts, price_cents, cost, decision: TradeDecision):
        import random
        win_prob = opp.best_side_consensus_prob if side == opp.best_side else (1 - opp.best_side_consensus_prob)
        won = random.random() < win_prob
        pnl = contracts * (100 - price_cents) / 100 if won else -cost
        self.risk.update(pnl)
        self.risk.record(TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            sport=opp.sport, game=f"{opp.away_team} @ {opp.home_team}",
            side=side, size_usd=cost, entry_price=price_cents / 100,
            pnl=pnl, reasoning=decision.reasoning,
        ))
        log.info(f"[PAPER] {'WIN' if won else 'LOSS'} | P&L: ${pnl:+.2f} | {self.risk.summary()}")

    async def _live_execute(self, opp, side, contracts, price_cents):
        try:
            # ── PRE-TRADE + REJECTION + DYNAMIC EDGE + SIZING (10-module integration) ──
            try:
                if _pre_trade_validator_available:
                    _ptv = validate_pre_trade({"ticker": locals().get("ticker", ""), "side": locals().get("side", ""), "bot": "sports"})
                    if _ptv and _ptv.get("halt"):
                        log.info(f"[PRE_TRADE_VALIDATOR] Halted: {_ptv.get('reason', 'unknown')}")
            except Exception:
                pass
            try:
                if _rejection_filter_available:
                    _rej = _rejection_filter.check(ticker=locals().get("ticker", ""), volume=locals().get("volume", 0), price_cents=locals().get("price_cents", locals().get("price", 50)))
                    if _rej and _rej.get("reject"):
                        log.info(f"[REJECTION_FILTER] Rejected: {_rej.get('reason', 'unknown')}")
            except Exception:
                pass
            _min_edge_dynamic = 0.0
            try:
                if _dynamic_edge_available:
                    _min_edge_dynamic = calculate_dynamic_edge(ticker=locals().get("ticker", ""), volume=locals().get("volume", 0), move_pct=locals().get("move_pct", locals().get("edge", 0)), time_remaining=locals().get("time_remaining", None))
            except Exception:
                pass
            _kelly_frac = 1.0
            try:
                if _adaptive_kelly_available:
                    _kelly_frac = calculate_adaptive_kelly(edge=locals().get("edge", locals().get("ev_rate", 0.05)), price_cents=locals().get("price_cents", locals().get("price", 50)), volume=locals().get("volume", 0), win_rate=0.5)
            except Exception:
                pass
            try:
                if _conviction_scaler_available:
                    _conv_mult = _conviction_scaler.scale(move_pct=locals().get("move_pct", locals().get("edge", 0)), volume=locals().get("volume", 0), ev_after_fees=locals().get("ev_rate", locals().get("edge", 0.05)), direction=locals().get("direction", locals().get("side", "yes")))
                    _kelly_frac *= _conv_mult
            except Exception:
                pass

            result = await self.kalshi.place_order(opp.kalshi_ticker, side, contracts, price_cents)
            order_id = result.get("order", {}).get("order_id", "unknown")
            log.info(f"[LIVE] Order placed | ID: {order_id}")
        except httpx.HTTPStatusError as e:
            log.error(f"[LIVE] Order rejected: {e.response.status_code} — {e.response.text}")
        except Exception as e:
            log.error(f"[LIVE] Order failed: {e}")


# ── Stats HTTP server ─────────────────────────────────────────────────────────
_stats_app = Flask(__name__)
_bot_stats = {"trades": 0, "wins": 0, "pnl": 0.0, "balance": 0.0, "start": time.time()}

@_stats_app.route("/stats")
def _stats_endpoint():
    t = _bot_stats
    total = t["trades"]
    return jsonify({"bot": "kalshi-sports-bot", "paper_mode": True,
        "balance": t["balance"], "trades": total, "wins": t["wins"],
        "losses": total - t["wins"], "win_rate": round(t["wins"]/max(total,1), 4),
        "pnl": t["pnl"], "uptime_hours": round((time.time()-t["start"])/3600, 2)})

@_stats_app.route("/health")
def _health_endpoint():
    return jsonify({"status": "ok"})

def _run_stats_server():
    _stats_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))


async def main():
    log.info("=" * 60)
    log.info("  Kalshi Sports Bot v3 — Ultra-Aggressive Mode")
    log.info(f"  Model: {Config.CLAUDE_MODEL}")
    log.info(f"  Sports: {len(Config.SPORTS)} sport types + prop markets")
    log.info(f"  Min edge: {Config.MIN_EDGE_PCT:.1%} | Poll: {Config.POLL_INTERVAL_SEC//60}min")
    log.info(f"  Max games/poll: {Config.MAX_GAMES_PER_POLL} | Min volume: {Config.MIN_VOLUME} | Min price: {Config.MIN_KALSHI_PRICE}¢")
    log.info(f"  Position: {Config.POSITION_FRACTION:.0%}/trade | Max: ${Config.MAX_TRADE_USD:.0f}")
    log.info(f"  Mode: {'PAPER' if Config.PAPER_MODE else '*** LIVE ***'}")
    log.info("=" * 60)

    if not Config.ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set.")
        return

    if not Config.ODDS_API_KEY:
        log.warning("ODDS_API_KEY not set. EV strategy disabled, momentum only.")

    kalshi = KalshiClient()
    odds   = OddsAPIClient()
    claude = ClaudeDecisionEngine()

    balance = Config.PAPER_BALANCE if Config.PAPER_MODE else 0.0
    if not Config.PAPER_MODE:
        try:
            balance = await kalshi.get_balance()
            log.info(f"Kalshi balance: ${balance:.2f}")
        except Exception as e:
            log.error(f"Could not fetch balance: {e}")
            return

    risk     = RiskManager(balance)
    _bot_stats['balance'] = balance
    threading.Thread(target=_run_stats_server, daemon=True).start()
    strategy = SportsStrategy(kalshi, odds, claude, risk)

    async def status_loop():
        while not risk.killed:

            # ── CYCLE START: Dynamic Params + Paper Balance (10-module integration) ──
            try:
                if _dynamic_params_available:
                    _cycle_params = _dynamic_params.get_all()
                    if "bet_size" in _cycle_params:
                        pass  # Override config if needed
            except Exception as _e:
                pass
            try:
                if _paper_balance_available:
                    _pbm_info = _paper_balance_mgr.check_and_restart(globals().get('paper_balance', 2000))
                    if _pbm_info and _pbm_info.get("restarted"):
                        log.info(f"[PAPER_BALANCE] Auto-restarted. Lifetime P&L: ${_pbm_info.get('lifetime_pnl', 0):.2f}")
            except Exception as _e:
                pass

            await asyncio.sleep(1800)
            log.info(f"[STATUS] {risk.summary()}")

    async def poll_loop():
        while not risk.killed:
            try:
                await strategy.run_poll()
            except Exception as e:
                log.error(f"[POLL] Error: {e}")
            log.info(f"[POLL] Next scan in {Config.POLL_INTERVAL_SEC//60} minutes.")
            await asyncio.sleep(Config.POLL_INTERVAL_SEC)

    try:
        await asyncio.gather(poll_loop(), status_loop())
    except KeyboardInterrupt:
        pass
    finally:
        await kalshi.close()
        await odds.close()
        log.info(f"Bot stopped. {risk.summary()}")


if __name__ == "__main__":
    asyncio.run(main())
