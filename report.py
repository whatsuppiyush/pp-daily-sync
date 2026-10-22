"""Daily summary."""
import argparse, datetime as dt, html, json, os, urllib.parse, urllib.request
from core import DB, ENV

LISTS = [  # list, source prefix in leads.first_source, schedule note
    ('producthunt', 'engine:producthunt', 'daily'), ('launches', 'engine:launches', 'Mondays'),
    ('revenue', 'engine:trustmrr', 'Tuesdays'), ('chrome', 'engine:chrome', 'daily'),
    ('shopify', 'engine:shopify', 'daily'), ('funded', 'engine:yc', '1st of the month'),
    ('showhn', 'engine:showhn', 'daily'), ('play', 'engine:play', 'daily'), ('stores', 'engine:stores', 'Mon + Thu'),
    ('hiring', 'engine:wellfound', 'Wed + Sat'), ('firefox', 'engine:firefox', 'Sundays'), ('yelp', 'engine:yelp', 'daily')]
TITLE = {'producthunt': 'Product Hunt', 'launches': 'Launch directories', 'revenue': 'TrustMRR', 'funded': 'YC',
         'chrome': 'Chrome Web Store', 'shopify': 'Shopify App Store', 'showhn': 'Show HN', 'play': 'Google Play',
         'stores': 'Shopify stores (Meta pixel)', 'hiring': 'Wellfound hiring', 'firefox': 'Firefox add-ons', 'yelp': 'Yelp local businesses'}
GITHUB_FREE_MIN, APIFY_PLAN_USD, GUMLOOP_USD_PER_CREDIT = 2000, 5.0, 0.005
BAD = ('invalid', 'disabled', 'disposable', 'spamtrap', 'inbox_full')


def env(k):
    return ENV.get(k) or os.environ.get(k) or ''


def get_json(url, headers=None, timeout=30):
    try:
        return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'pp-daily-sync', **(headers or {})}), timeout=timeout))
    except Exception as e:
        return {'_error': str(e)[:120]}


def emails_block(prefix, since, extra_where=''):
    return DB.execute(f"""
      select count(*) as emails, count(*) filter (where contact_type = 'founder') as founder,
             count(*) filter (where reoon_status = 'safe') as safe, count(*) filter (where reoon_status = 'role_account') as role,
             count(*) filter (where reoon_status = 'catch_all') as catch_all, count(*) filter (where reoon_status = 'unknown') as unknown,
             count(*) filter (where reoon_status in {BAD}) as bad, count(*) filter (where reoon_status is null) as unchecked,
             count(*) filter (where reoon_safe) as safe_to_send
      from leads l where first_source like %s and created_at >= %s {extra_where}""", (prefix + '%', since)).fetchone()


def build(hours):
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    runs = DB.execute("select id, list, stats from engine_runs where started_at >= %s and ok is not false", (since,)).fetchall()
    spend = DB.execute("""select e.list, s.provider, sum(s.units) units, sum(coalesce(s.usd, 0)) usd
                          from spend_log s join engine_runs e on e.id = s.run_id where e.started_at >= %s group by 1, 2""", (since,)).fetchall()
    cost = {}
    for r in spend: cost.setdefault(r['list'], {})[r['provider']] = (float(r['units']), float(r['usd']))
    out = [f"<b>Daily sync · {dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))):%a %d %b, %H:%M} IST</b>"]
    tot_found = tot_safe = 0
    for lst, prefix, when in LISTS:
        rs = [r for r in runs if r['list'] == lst]
        if not rs:
            out.append(f"\n<b>{TITLE[lst]}</b>: not scheduled today ({when})"); continue
        st = {}
        for r in rs:
            for k, v in (r['stats'] or {}).items():
                if isinstance(v, (int, float)): st[k] = st.get(k, 0) + v
        e = emails_block(prefix, since)
        tot_found += e['emails']; tot_safe += e['safe_to_send']
        lines = [f"\n<b>{TITLE[lst]}</b>"]
        if lst == 'producthunt':
            d = next((r['stats'].get('ph_day') for r in reversed(rs) if (r['stats'] or {}).get('ph_day')), None)
            if not d:
                day = DB.execute("""select signals->>'ph_day' d from companies where 'producthunt' = any(lists)
                                    and updated_at >= %s and signals ? 'ph_day' order by 1 desc limit 1""", (since,)).fetchone()
                d = day['d'] if day else '?'
            hi = DB.execute("""select count(*) n from companies where signals->>'ph_day' = %s and (signals->>'upvotes')::int > 30""", (d,)).fetchone()['n']
            e30 = emails_block(prefix, since, f"and company_domain in (select domain from companies where signals->>'ph_day' = '{d}' and (signals->>'upvotes')::int > 30)")
            lines.append(f"Launch day {d}: {st.get('items', 0)} launches, {st.get('items', 0) - st.get('no_website', 0)} with a website")
            lines.append(f"Over 30 upvotes: {hi} launches → {e30['emails']} emails → <b>{e30['safe_to_send']} safe to send</b>")
        else:
            lines.append(f"Scraped {st.get('items', 0)}, with a website {st.get('items', 0) - st.get('no_website', 0)}")
        lines.append(f"Companies: {st.get('crawled', 0)} new, {st.get('reused_free', 0)} already known (free)"
                     + (f", {st.get('skipped_big', 0)} big skipped" if st.get('skipped_big') else ''))
        lines.append(f"Emails found: {e['emails']} ({e['founder']} founder, {e['emails'] - e['founder']} inbox)")
        lines.append(f"Reoon: {e['safe']} safe · {e['role']} role · {e['catch_all']} catch-all · {e['unknown']} unknown · {e['bad']} bad"
                     + (f" · {e['unchecked']} not checked yet" if e['unchecked'] else ''))
        lines.append(f"<b>Safe to send: {e['safe_to_send']}</b>")
        c = cost.get(lst, {})
        lines.append('Cost: ' + (', '.join(f"{p} ${u:.3f}" for p, (n, u) in c.items() if u) or '$0') + f" + {e['emails'] - e['unchecked']} Reoon credits")
        out += lines

    # ---- spend and budgets
    month0 = dt.datetime.now(dt.timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    m = {r['provider']: (float(r['units']), float(r['usd'] or 0)) for r in DB.execute(
        "select provider, sum(units) units, sum(usd) usd from spend_log where at >= %s group by 1", (month0,)).fetchall()}
    t = {r['provider']: (float(r['units']), float(r['usd'] or 0)) for r in DB.execute(
        "select provider, sum(units) units, sum(usd) usd from spend_log where at >= %s group by 1", (since,)).fetchall()}
    warn = []
    out.append("\n<b>Spend</b> (today · this month)")
    out.append(f"Apify: ${t.get('apify', (0, 0))[1]:.3f} · ${m.get('apify', (0, 0))[1]:.2f}")
    out.append(f"Reoon credits: {int(t.get('reoon', (0, 0))[0])} · {int(m.get('reoon', (0, 0))[0])}")
    if m.get('treg'): out.append(f"treg: ${t.get('treg', (0, 0))[1]:.3f} · ${m['treg'][1]:.2f}")

    gl_today = gl_month = None
    if env('GUMLOOP_API_KEY') and env('GUMLOOP_USER_ID') and env('GUMLOOP_AGENT_ID'):
        s = get_json(f"https://api.gumloop.com/api/v1/agents/{env('GUMLOOP_AGENT_ID')}/sessions"
                     f"?user_id={env('GUMLOOP_USER_ID')}&page_size=100", {'Authorization': f"Bearer {env('GUMLOOP_API_KEY')}"})
        ss = s.get('sessions') or []
        cred = lambda s_: float((s_.get('usage') or {}).get('credit_cost') or 0)
        gl_today = sum(cred(x) for x in ss if x.get('created_at', '') >= (since.isoformat()[:19]))
        gl_month = sum(cred(x) for x in ss if x.get('created_at', '') >= month0.isoformat()[:19])
        out.append(f"Gumloop: {gl_today:.0f} credits (${gl_today * GUMLOOP_USD_PER_CREDIT:.2f}) · {gl_month:.0f} (${gl_month * GUMLOOP_USD_PER_CREDIT:.2f})")
    cash_month = sum(v[1] for v in m.values()) + (gl_month or 0) * GUMLOOP_USD_PER_CREDIT
    out.append(f"<b>Total this month: ${cash_month:.2f}</b>")

    out.append("\n<b>Budgets left</b>")
    toks = [t for t in (env('APIFY_TOKENS') or env('APIFY_TOKEN')).split(',') if t.strip()]
    left = lim_all = 0.0
    for tk in toks:
        ap = get_json(f"https://api.apify.com/v2/users/me/limits?token={tk.strip()}").get('data') or {}
        if ap:
            lim = ap['limits']['maxMonthlyUsageUsd'] or APIFY_PLAN_USD
            lim_all += lim; left += lim - ap['current']['monthlyUsageUsd']
    if lim_all:
        out.append(f"Apify: ${left:.2f} of ${lim_all:.0f} left this month ({len(toks)} accounts)")
        if left / lim_all <= 0.2: warn.append(f'Apify: only ${left:.2f} left across all accounts')
    rb = get_json(f"https://emailverifier.reoon.com/api/v1/check-account-balance/?key={env('REOON_API_KEY')}")
    if 'remaining_daily_credits' in rb:
        out.append(f"Reoon: {rb['remaining_daily_credits']} daily + {rb['remaining_instant_credits']:,} purchased credits")
        if int(rb['remaining_instant_credits']) < 2000: warn.append('Reoon purchased credits below 2,000')
    gh_tok, repo = env('GITHUB_TOKEN'), env('GITHUB_REPOSITORY') or 'whatsuppiyush/pp-daily-sync'
    if gh_tok:
        runs_ = get_json(f"https://api.github.com/repos/{repo}/actions/runs?per_page=100&created=%3E%3D{month0:%Y-%m-%d}",
                         {'Authorization': f'Bearer {gh_tok}', 'Accept': 'application/vnd.github+json'}).get('workflow_runs') or []
        mins = 0
        for r in runs_:
            a, b = r.get('run_started_at'), r.get('updated_at')
            if a and b:
                mins += max(1, int((dt.datetime.fromisoformat(b.rstrip('Z')) - dt.datetime.fromisoformat(a.rstrip('Z'))).total_seconds() // 60) + 1)
        public = get_json(f"https://api.github.com/repos/{repo}", {'Authorization': f'Bearer {gh_tok}',
                          'Accept': 'application/vnd.github+json'}).get('private') is False
        if public:   # public repo: standard runners are free and unmetered, so there is no budget to warn about
            out.append(f"GitHub Actions: ~{mins} minutes run this month (public repo, free and unlimited)")
        else:
            out.append(f"GitHub Actions: ~{mins} of {GITHUB_FREE_MIN} free minutes used this month")
            if mins >= 0.8 * GITHUB_FREE_MIN: warn.append(f'GitHub minutes at {mins / GITHUB_FREE_MIN:.0%}: add the upvote floor or make the repo public')

    ready = DB.execute("""select count(*) n from leads l where l.reoon_safe and cardinality(l.fit_products) > 0
                          and not exists (select 1 from instantly_pushes p where p.email = l.email)
                          and not exists (select 1 from suppression s where s.email = l.email)""").fetchone()['n']
    out.append(f"\nToday: {tot_found} emails found, <b>{tot_safe} safe to send</b>. Waiting to push (all days): {ready}")
    if warn: out.append('\n⚠️ ' + '\n⚠️ '.join(warn))
    return '\n'.join(out)


def send(text):
    tok, chat = env('TELEGRAM_BOT_TOKEN'), env('TELEGRAM_CHAT_ID')
    if not tok or not chat:
        print('no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID; report printed only'); return
    body = urllib.parse.urlencode({'chat_id': chat, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': 'true'}).encode()
    r = json.load(urllib.request.urlopen(f'https://api.telegram.org/bot{tok}/sendMessage', body, timeout=30))
    print('telegram:', 'sent' if r.get('ok') else r)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--hours', type=int, default=20, help='window: runs started in the last N hours')
    ap.add_argument('--dry', action='store_true', help='print only')
    a = ap.parse_args()
    text = build(a.hours)
    print(html.unescape(text.replace('<b>', '').replace('</b>', '')))
    if not a.dry: send(text)
