"""
13F Hedge Fund Tracker

Pulls the two most recent 13F-HR filings for each fund in funds.csv from the
SEC EDGAR API, compares holdings quarter over quarter, and writes a report on
what the funds collectively bought, sold, and hold.

Usage:
    export SEC_USER_AGENT="Your Name your@email.com"   # required by the SEC
    python tracker.py                    # latest quarter
    python tracker.py --quarter 2026-03-31
"""
import argparse
import csv
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import requests
from defusedxml import ElementTree as ET

ROOT = Path(__file__).parent
CACHE = ROOT / ".cache"
SPLIT_RATIOS = {2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 40, 50}

session = requests.Session()


# ── EDGAR access ───────────────────────────────────────────────────────────────
def sec_get(url: str) -> bytes:
    """GET from SEC with caching and the polite rate limit (<10 requests/sec)."""
    CACHE.mkdir(exist_ok=True)
    cached = CACHE / url.replace("https://", "").replace("/", "_")
    # Filing documents never change; only the submissions index needs refreshing.
    if cached.exists() and "submissions" not in url:
        return cached.read_bytes()
    time.sleep(0.15)
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    cached.write_bytes(resp.content)
    return resp.content


def list_13f_filings(cik: int) -> dict:
    """Return {report_period: accession_number} for a fund's original 13F-HR filings."""
    data = json.loads(sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json"))
    recent = data["filings"]["recent"]
    filings = {}
    for form, period, acc in zip(recent["form"], recent["reportDate"], recent["accessionNumber"]):
        if form == "13F-HR" and period not in filings:  # newest first; skip amendments
            filings[period] = acc
    return filings


def fetch_holdings(cik: int, accession: str) -> dict:
    """Return {cusip: {"name", "shares", "value"}} for common-stock positions in a filing.

    Option positions (puts/calls) and bond principal amounts are excluded. Rows split
    across sub-managers are summed per CUSIP.
    """
    folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}"
    index = json.loads(sec_get(f"{folder}/index.json"))
    xml_files = [f["name"] for f in index["directory"]["item"]
                 if f["name"].lower().endswith(".xml") and f["name"].lower() != "primary_doc.xml"]
    holdings = defaultdict(lambda: {"name": "", "shares": 0, "value": 0})
    for xml_name in xml_files:
        root = ET.fromstring(sec_get(f"{folder}/{xml_name}"))
        for row in root.iter():
            if not row.tag.endswith("infoTable"):
                continue
            fields = {child.tag.split("}")[1]: child for child in row}
            if "putCall" in fields:
                continue
            amount = {c.tag.split("}")[1]: c.text for c in fields["shrsOrPrnAmt"]}
            if amount.get("sshPrnamtType") != "SH":
                continue
            cusip = fields["cusip"].text.strip().upper()
            h = holdings[cusip]
            h["name"] = fields["nameOfIssuer"].text.strip()
            h["shares"] += int(float(amount["sshPrnamt"]))
            h["value"] += int(float(fields["value"].text))
    # Values should be in dollars, but some filers still report in thousands (the
    # pre-2023 convention). A median implied share price under $1 gives that away.
    prices = sorted(h["value"] / h["shares"] for h in holdings.values() if h["shares"])
    if prices and prices[len(prices) // 2] < 1:
        for h in holdings.values():
            h["value"] *= 1000
    return dict(holdings)


# ── CUSIP → ticker via OpenFIGI (free, no key) ─────────────────────────────────
def lookup_tickers(cusips: list) -> dict:
    """Map CUSIPs to U.S. tickers. Codes starting with a letter are CINS numbers
    (foreign issuers such as CRH or ASML), which OpenFIGI looks up separately."""
    tickers = {}
    for i in range(0, len(cusips), 10):
        batch = cusips[i:i + 10]
        jobs = [{"idType": "ID_CINS" if c[0].isalpha() else "ID_CUSIP", "idValue": c, "exchCode": "US"}
                for c in batch]
        try:
            for _ in range(2):
                resp = requests.post("https://api.openfigi.com/v3/mapping", json=jobs, timeout=30)
                if resp.status_code != 429:
                    break
                time.sleep(60)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  OpenFIGI lookup failed ({e}); continuing without some tickers", file=sys.stderr)
            break
        for cusip, result in zip(batch, resp.json()):
            if result.get("data") and result["data"][0].get("ticker"):
                tickers[cusip] = result["data"][0]["ticker"]
        time.sleep(2.5)  # unauthenticated limit: 25 requests/minute
    return tickers


# ── Analysis ───────────────────────────────────────────────────────────────────
def prior_quarter(period: str) -> str:
    y, m, _ = map(int, period.split("-"))
    return {3: f"{y - 1}-12-31", 6: f"{y}-03-31", 9: f"{y}-06-30", 12: f"{y}-09-30"}[m]


def quarter_label(period: str) -> str:
    y, m, _ = map(int, period.split("-"))
    return f"{y}-Q{m // 3}"


def is_split(old: int, new: int) -> bool:
    """True when the share change is an exact stock-split ratio rather than a trade."""
    if old == 0 or new == 0:
        return False
    big, small = max(old, new), min(old, new)
    return big % small == 0 and big // small in SPLIT_RATIOS


def classify(prev: dict, curr: dict) -> dict:
    """Return {cusip: action} where action is NEW, ADD, TRIM, or EXIT."""
    actions = {}
    for cusip in set(prev) | set(curr):
        old = prev.get(cusip, {}).get("shares", 0)
        new = curr.get(cusip, {}).get("shares", 0)
        if old == new or is_split(old, new):
            continue
        if old == 0:
            actions[cusip] = "NEW"
        elif new == 0:
            actions[cusip] = "EXIT"
        elif new > old * 1.05:
            actions[cusip] = "ADD"
        elif new < old * 0.95:
            actions[cusip] = "TRIM"
    return actions


def fmt_money(v: float) -> str:
    if v >= 1e9:
        return f"${v / 1e9:,.1f}B"
    return f"${v / 1e6:,.0f}M"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quarter", help="report period end date, e.g. 2026-06-30")
    args = parser.parse_args()

    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        sys.exit('Set SEC_USER_AGENT to "Your Name your@email.com" (the SEC requires a contact).')
    session.headers["User-Agent"] = user_agent

    with open(ROOT / "funds.csv") as f:
        funds = [(row["name"], int(row["cik"])) for row in csv.DictReader(f)]

    print("Fetching filing lists...")
    filing_lists = {name: list_13f_filings(cik) for name, cik in funds}
    quarter = args.quarter or Counter(
        max(fl) for fl in filing_lists.values() if fl).most_common(1)[0][0]
    prev_q = prior_quarter(quarter)
    print(f"Quarter: {quarter_label(quarter)} (vs {quarter_label(prev_q)})")

    portfolios, changes, skipped = {}, {}, []
    for name, cik in funds:
        fl = filing_lists[name]
        if quarter not in fl or prev_q not in fl:
            skipped.append(name)
            continue
        print(f"  {name}")
        curr = fetch_holdings(cik, fl[quarter])
        prev = fetch_holdings(cik, fl[prev_q])
        portfolios[name] = curr
        changes[name] = (prev, classify(prev, curr))

    # Aggregate across funds
    names = {}
    buyers, sellers, holders = defaultdict(list), defaultdict(list), defaultdict(list)
    held_value = Counter()
    new_positions = []
    for fund, curr in portfolios.items():
        total = sum(h["value"] for h in curr.values()) or 1
        for cusip, h in curr.items():
            names[cusip] = h["name"]
            holders[cusip].append(fund)
            held_value[cusip] += h["value"]
        prev, actions = changes[fund]
        for cusip, action in actions.items():
            names.setdefault(cusip, prev.get(cusip, {}).get("name", cusip))
            if action in ("NEW", "ADD"):
                buyers[cusip].append((fund, action))
            else:
                sellers[cusip].append((fund, action))
            if action == "NEW":
                new_positions.append((curr[cusip]["value"] / total, fund, cusip, curr[cusip]["value"]))

    def ranked(group):
        return sorted(group.items(), key=lambda kv: (-len(kv[1]), -held_value[kv[0]]))[:15]

    top_buys, top_sells = ranked(buyers), ranked(sellers)
    top_held = sorted(holders.items(), key=lambda kv: (-len(kv[1]), -held_value[kv[0]]))[:15]
    new_positions.sort(reverse=True)
    top_new = new_positions[:15]

    report_cusips = {c for c, _ in top_buys + top_sells + top_held} | {c for _, _, c, _ in top_new}
    print(f"Looking up {len(report_cusips)} tickers on OpenFIGI...")
    tickers = lookup_tickers(sorted(report_cusips))

    def label(cusip):
        t = tickers.get(cusip)
        return f"**{t}** ({names[cusip].title()})" if t else names[cusip].title()

    def fund_list(entries):
        return ", ".join(f"{f} ({a.lower()})" for f, a in entries)

    # ── Write report ──
    ql, pl = quarter_label(quarter), quarter_label(prev_q)
    lines = [
        f"# 13F Report: {ql}",
        "",
        f"Holdings changes from {pl} to {ql} across {len(portfolios)} hedge funds, "
        f"from 13F-HR filings on SEC EDGAR. Generated {date.today().isoformat()}.",
        "",
        "13F filings are due 45 days after quarter end, so they show positions as of the "
        "quarter's last day, not today. They cover U.S. long equity positions only: no shorts, "
        "no cash, no non-U.S. holdings. Option positions are excluded below.",
        "",
        "## Consensus buys",
        "Securities the most funds opened or added to (by 5%+ of shares).",
        "",
        "| Security | Funds buying | Who |",
        "|---|---|---|",
    ]
    lines += [f"| {label(c)} | {len(e)} | {fund_list(e)} |" for c, e in top_buys]
    lines += ["", "## Consensus sells", "Securities the most funds exited or trimmed (by 5%+ of shares).", "",
              "| Security | Funds selling | Who |", "|---|---|---|"]
    lines += [f"| {label(c)} | {len(e)} | {fund_list(e)} |" for c, e in top_sells]
    lines += ["", "## Largest new positions", "New positions ranked by share of the fund's 13F portfolio.", "",
              "| Fund | Security | Value | % of portfolio |", "|---|---|---|---|"]
    lines += [f"| {f} | {label(c)} | {fmt_money(v)} | {w:.1%} |" for w, f, c, v in top_new]
    lines += ["", "## Most widely held", "", "| Security | Funds holding | Combined value |", "|---|---|---|"]
    lines += [f"| {label(c)} | {len(h)} | {fmt_money(held_value[c])} |" for c, h in top_held]
    lines += ["", "## Fund summary", "",
              "| Fund | 13F value | Positions | Top-10 concentration | New | Exits |", "|---|---|---|---|---|---|"]
    for fund, curr in sorted(portfolios.items(), key=lambda kv: -sum(h["value"] for h in kv[1].values())):
        values = sorted((h["value"] for h in curr.values()), reverse=True)
        total = sum(values) or 1
        acts = Counter(changes[fund][1].values())
        lines.append(f"| {fund} | {fmt_money(total)} | {len(values)} | {sum(values[:10]) / total:.0%} "
                     f"| {acts['NEW']} | {acts['EXIT']} |")
    if skipped:
        lines += ["", f"_Not included (no filing for both {pl} and {ql}): {', '.join(skipped)}._"]

    (ROOT / "reports").mkdir(exist_ok=True)
    out = ROOT / "reports" / f"{ql}.md"
    out.write_text("\n".join(lines) + "\n")

    (ROOT / "data").mkdir(exist_ok=True)
    with open(ROOT / "data" / f"{ql}_changes.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fund", "cusip", "ticker", "issuer", "action", "prev_shares", "curr_shares", "curr_value_usd"])
        for fund, (prev, actions) in sorted(changes.items()):
            curr = portfolios[fund]
            for cusip, action in sorted(actions.items()):
                w.writerow([fund, cusip, tickers.get(cusip, ""), names.get(cusip, ""), action,
                            prev.get(cusip, {}).get("shares", 0), curr.get(cusip, {}).get("shares", 0),
                            curr.get(cusip, {}).get("value", 0)])
    print(f"Wrote {out.relative_to(ROOT)} and data/{ql}_changes.csv")


if __name__ == "__main__":
    main()
