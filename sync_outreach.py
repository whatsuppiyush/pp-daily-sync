"""Pull every lead's campaign state from the primary Instantly workspace into Neon's `outreach` ledger.

    python sync_outreach.py            # full sync (daily.sh runs it before push_lists.py)

One row per (email, campaign): which product it pitched, when it was added, last email sent, replies, the reply label,
and an `outcome` the router reads (queued, in_sequence, completed, interested, not_interested, ooo, replied, bounced,
unsubscribed). Bounces and unsubscribes also land in `suppression`, so no product ever emails them again.
Rows are only rewritten when something changed (Neon free plan is 512 MB). Read-only towards Instantly.
"""
import json, re, time, urllib.error, urllib.request
from core import DB, ENV

API = 'https://api.instantly.ai/api/v2'
# campaign name prefix -> product; a campaign whose name matches none is synced with product 'other'
PRODUCTS = [('BacklinkBot Launch', 'backlinkbot_launch'), ('BacklinkBot - Launch + tool', 'backlinkbot_dfy'), ('BacklinkBot', 'backlinkbot'), ('XAutoDM', 'xautodm'),
            ('ChatterAPI', 'chatterapi'), ('HearBuyers - Agencies', 'hearbuyers_agencies'), ('HearBuyers', 'hearbuyers'), ('Subreddit Marketing', 'subredditmarketing'),
            ('HookAds', 'hookads'), ('Yelp - AI ads', 'yelp_ads'), ('Yelp', 'yelp_local')]
INTEREST = {1: 'interested', 2: 'interested', 3: 'interested', 4: 'interested',      # interested, meeting booked/done, won
            0: 'ooo', -1: 'not_interested', -2: 'not_interested', -3: 'not_interested'}  # ooo, not interested, wrong person, lost

SCHEMA = """
create table if not exists outreach (
  email           text not null references leads(email),
  campaign_id     text not null,
  campaign_name   text,
  product         text,
  instantly_lead  text,
  added_at        timestamptz,
  last_step       text,          -- Instantly step id of the last email sent, e.g. 0_2_0 = email 3
  last_contact_at timestamptz,
  replies         int not null default 0,
  last_reply_at   timestamptz,
  interest        int,           -- Instantly lt_interest_status
  lead_status     int,           -- 1 active, 3 completed, -1 bounced, -2 unsubscribed
  outcome         text not null,
  dr_at_push      int,           -- set by the router when it pushes, so a later DR change is visible
  route_reason    text,          -- set by the router: why this lead got this product
  synced_at       timestamptz not null default now(),
  primary key (email, campaign_id));
create index if not exists outreach_email_idx on outreach (email);
create index if not exists outreach_product_idx on outreach (product, outcome);
"""


def call(path, body=None):
    req = urllib.request.Request(API + path, json.dumps(body).encode() if body is not None else None,
                                 {'Authorization': f"Bearer {ENV['INSTANTLY_API_KEY']}", 'Content-Type': 'application/json',
                                  'User-Agent': 'pp-daily-sync/1.0'})
    for attempt in range(5):
        try:
            return json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 4: raise
            time.sleep(2 ** attempt * 3)


AUTO = re.compile(r'out of (the )?office|auto(matic)?[- ]?reply|autoreply|on (annual )?leave|vacation|ticket (#|number)|'
                  r'we (have )?received your (email|message|request)|thanks for (contacting|reaching out to) (us|our)|'
                  r'do not reply|no longer (with|at)|support request', re.I)
QUOTE = re.compile(r'\n(On .{5,120}wrote:|-{2,} ?Original Message|From: .+\nSent: )', re.S)


def sync_replies():
    """Every reply in the primary workspace into `replies` (workspace 'primary'): text without the quoted thread, plus
    what is needed to answer it in Instantly (thread_id, the inbox it came to). Insert-only, so it stays small."""
    for col in ('thread_id text', 'eaccount text', 'from_address text', 'step text'):
        DB.execute(f'alter table replies add column if not exists {col}')
    have = {r['id'] for r in DB.execute("select id from replies where workspace = 'primary'").fetchall()}
    new, after = [], None
    while True:
        d = call('/emails?email_type=received&limit=100' + (f'&starting_after={after}' if after else ''))
        items = d.get('items') or []
        fresh = [e for e in items if e['id'] not in have]
        new += fresh; after = d.get('next_starting_after')
        if not after or not items or len(fresh) < len(items): break    # newest first: stop at the first page we already hold
    for e in new:
        body = e.get('body') or {}
        text = body.get('text') or re.sub(r'<[^>]+>', ' ', body.get('html') or '') if isinstance(body, dict) else str(body)
        text = QUOTE.split(text)[0].strip()[:5000]
        lab = INTEREST.get(int(e['i_status'])) if str(e.get('i_status') or '').lstrip('-').isdigit() else None
        DB.execute("""insert into replies (id, workspace, email, campaign_id, received_at, label, auto_reply, subject, body,
                                           thread_id, eaccount, from_address, step)
                      values (%s, 'primary', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict (id) do nothing""",
                   (e['id'], (e.get('lead') or e.get('from_address_email') or '').lower(), e.get('campaign_id'), e.get('timestamp_email'),
                    lab, bool(AUTO.search((e.get('subject') or '') + ' ' + text[:600])), e.get('subject'), text,
                    e.get('thread_id'), e.get('eaccount'), e.get('from_address_email'), e.get('step')))
    return len(new)


def outcome(l):
    if l.get('status') == -1: return 'bounced'
    if l.get('status') == -2: return 'unsubscribed'
    if l.get('lt_interest_status') in INTEREST: return INTEREST[l['lt_interest_status']]
    if l.get('email_reply_count'): return 'replied'
    if l.get('status') == 3: return 'completed'
    return 'in_sequence' if l.get('timestamp_last_contact') else 'queued'


def product_of(name):
    return next((p for prefix, p in PRODUCTS if name.startswith(prefix)), 'other')


def main():
    for stmt in SCHEMA.split(';'):
        if stmt.strip(): DB.execute(stmt)
    campaigns = call('/campaigns?limit=100')['items']
    known = {r['email'] for r in DB.execute('select email from leads').fetchall()}
    total = changed = missing = 0
    for c in campaigns:
        rows, after = [], None
        while True:
            d = call('/leads/list', {'campaign': c['id'], 'limit': 100, **({'starting_after': after} if after else {})})
            rows += d['items']; after = d.get('next_starting_after')
            if not after or not d['items']: break
        recs = []
        for l in rows:
            e = l['email'].strip().lower()
            if e not in known: missing += 1; continue          # pushed by hand from outside Neon: not ours to track
            recs.append((e, c['id'], c['name'], product_of(c['name']), l['id'], l.get('timestamp_created'),
                         (l.get('status_summary') or {}).get('lastStep', {}).get('stepID'), l.get('timestamp_last_contact'),
                         l.get('email_reply_count') or 0, l.get('timestamp_last_reply'), l.get('lt_interest_status'),
                         l.get('status'), outcome(l)))
        DB.execute('select 1')                                   # (re)connect before taking the raw connection
        with DB.conn.transaction(), DB.conn.cursor() as cur:     # one transaction: the pooler keeps us on one backend
            cur.execute('create temp table o_stage (like outreach including defaults) on commit drop')
            with cur.copy('copy o_stage (email, campaign_id, campaign_name, product, instantly_lead, added_at, last_step, '
                          'last_contact_at, replies, last_reply_at, interest, lead_status, outcome) from stdin') as cp:
                for r in recs: cp.write_row(r)
            cur.execute("""
              with up as (
                insert into outreach (email, campaign_id, campaign_name, product, instantly_lead, added_at, last_step,
                                      last_contact_at, replies, last_reply_at, interest, lead_status, outcome)
                select distinct on (email) email, campaign_id, campaign_name, product, instantly_lead, added_at, last_step,
                       last_contact_at, replies, last_reply_at, interest, lead_status, outcome from o_stage
                on conflict (email, campaign_id) do update set
                  campaign_name = excluded.campaign_name, product = excluded.product, instantly_lead = excluded.instantly_lead,
                  last_step = excluded.last_step, last_contact_at = excluded.last_contact_at, replies = excluded.replies,
                  last_reply_at = excluded.last_reply_at, interest = excluded.interest, lead_status = excluded.lead_status,
                  outcome = excluded.outcome, synced_at = now()
                where (outreach.product, outreach.campaign_name, outreach.last_step, outreach.last_contact_at, outreach.replies, outreach.interest,
                       outreach.lead_status, outreach.outcome) is distinct from
                      (excluded.product, excluded.campaign_name, excluded.last_step, excluded.last_contact_at, excluded.replies, excluded.interest,
                       excluded.lead_status, excluded.outcome)
                returning 1)
              select count(*) from up""")
            changed += cur.fetchone()['count']
        total += len(recs)
    DB.execute("""insert into suppression (email, reason)
                  select distinct email, case outcome when 'bounced' then 'Bounced' else 'Unsubscribed' end from outreach
                  where outcome in ('bounced', 'unsubscribed') on conflict (email) do nothing""")
    s = DB.execute("""select product, count(*) n, count(*) filter (where outcome in ('in_sequence', 'queued')) live,
                        count(*) filter (where outcome = 'interested') interested,
                        count(*) filter (where outcome = 'not_interested') said_no,
                        count(*) filter (where outcome in ('bounced', 'unsubscribed')) dead
                      from outreach group by 1 order by 2 desc""").fetchall()
    n_rep = sync_replies()
    print(f'replies: {n_rep} new')
    print(f'outreach sync: {len(campaigns)} campaigns, {total} leads, {changed} rows changed, {missing} not in Neon (skipped)')
    for r in s: print(f"  {r['product']:20} {r['n']:6} live {r['live']:5} interested {r['interested']:3} said no {r['said_no']:3} dead {r['dead']:4}")


if __name__ == '__main__':
    main()
