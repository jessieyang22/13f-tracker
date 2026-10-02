# 13F Hedge Fund Tracker

Every quarter, institutional investment managers with over $100M in U.S. equities must disclose their long positions to the SEC on Form 13F. This project pulls those filings for 24 well-known hedge funds and investors straight from the SEC EDGAR API, compares each fund's holdings to the prior quarter, and reports what the group collectively bought, sold and holds.

**Latest report: [2026-Q2](reports/2026-Q2.md)**

## What the report shows

| Section | Question it answers |
|---|---|
| Consensus buys | Which stocks did the most funds open or add to? |
| Consensus sells | Which stocks did the most funds exit or trim? |
| Largest new positions | Which new positions are the biggest bets, as a share of the fund's portfolio? |
| Most widely held | Which stocks appear in the most portfolios? |
| Fund summary | Each fund's 13F value, number of positions, top-10 concentration and turnover |

A full position-level change log for each quarter is saved in [`data/`](data/) as CSV.

## Q2 2026 highlights

- **SpaceX (SPCX)** was the most common new position: 8 of 23 funds opened one. At D1 Capital it is 62% of the 13F portfolio.
- **Alphabet, Amazon and Carvana** each drew buying from 7 funds.
- **TSMC, Meta and Broadcom** led selling, with 6 funds each exiting or trimming. Meta also had 4 funds buying, so the group disagreed on it.

## How it works

1. **SEC EDGAR submissions API** (`data.sec.gov/submissions`) lists each fund's filings. The script finds the original 13F-HR for the target quarter and the one before it.
2. **The filing's information table** (XML) is downloaded and parsed. Rows split across sub-managers are summed by CUSIP. Options and bond positions are excluded.
3. **Each position is classified** as NEW, ADD, TRIM or EXIT. ADD and TRIM require a 5%+ change in shares. Changes that exactly match a stock-split ratio are ignored.
4. **OpenFIGI API** maps CUSIPs to tickers.
5. **GitHub Actions** reruns the pipeline a few days after each 13F deadline and commits the new report, so the repo updates itself every quarter.

## Run it yourself

```bash
pip install -r requirements.txt
export SEC_USER_AGENT="Your Name your@email.com"   # the SEC requires a contact in every request
python tracker.py                                  # latest quarter
python tracker.py --quarter 2026-03-31             # a specific quarter
```

To change the funds tracked, edit [`funds.csv`](funds.csv). A fund's CIK (its SEC ID) is in the URL of its EDGAR page.

## Limitations

- 13Fs show positions as of quarter end and are filed up to 45 days later, so they are stale by the time they are public.
- They cover U.S.-listed long equity positions only. Shorts, cash, bonds, non-U.S. holdings and private investments are not included, so a fund's 13F value is not its AUM.
- Funds can request confidential treatment to delay disclosing a position.
- Some filers report values in thousands instead of dollars. The script detects this from the implied share price and converts.

---

*Built by Jessica Yang — Finance & Statistics, University of Florida*
