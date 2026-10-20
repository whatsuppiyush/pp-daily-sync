"""Decide which product each lead hears about next, and top up each Instantly campaign with it.

    python route_outreach.py                                  # dry run: what each campaign would get, sends nothing
    python route_outreach.py --apply                          # top every campaign up (Sunday refill)
    python route_outreach.py --apply --products backlinkbot,backlinkbot_dfy,backlinkbot_launch   # daily: new launches
    python route_outreach.py --show 20                        # also print sample decisions

Each campaign is topped up to TARGET_WEEKS of new leads (about 2 x daily_limit a week, since follow-ups share the
limit), counting leads already queued there and not yet emailed. Run sync_outreach.py first: it is the history.

Rules (agreed 2026-10-18):
  R1 fresh pool (real launch date in the last FRESH_DAYS) starts with BacklinkBot: DR < 30 -> Launch claim
     (BacklinkBot DFY "Launch + tool" until the claim campaign has listing URLs), DR 30+ -> Agent
  R2 back catalog starts with the products that fit, split evenly between them (hash of the email)
  R3 one live sequence per person; the next product only after GAP_DAYS of quiet since the last email
  R4 ladder: BacklinkBot -> XAutoDM -> HearBuyers -> Subreddit Marketing (traction) / ChatterAPI (dev tools)
  R5 at most MAX_PRODUCTS_180D products per person in any 180 days
  R6 one conversation per company: founder mailbox over shared inbox, and no company with a live sequence or a reply
  R7 interested or any unlabelled reply -> off the ladder; not interested -> skip only that product; bounce/unsub -> suppressed
  R8 HARD RULE (Piyush, 2026-10-20): nothing goes to Instantly unless Reoon marked it safe (leads.reoon_safe = true)
     within the last VERIFY_DAYS. Catch-all, unknown, never-checked and local-SMTP-only "valid" all wait.
     Every pushed lead carries custom variables reoonVerified / reoonStatus / reoonCheckedAt, so Instantly shows it too
Agencies (us_agency_owners_2025) get HearBuyers Agencies; Yelp businesses get Yelp AI ads (already advertising on
Yelp) or Yelp local listings (everyone else with a rating). Same R3-R8 gates for both.
"""
import argparse, collections, datetime as dt, hashlib, json, urllib.error, urllib.request
from core import DB, ENV
from push_lists import SOURCE_LINE

API = 'https://api.instantly.ai/api/v2'
FRESH_DAYS, GAP_DAYS, MAX_PRODUCTS_180D, VERIFY_DAYS, TARGET_WEEKS = 60, 30, 3, 120, 2
LAUNCH_CLAIM_READY = False      # flip once launch.backlinkbot.ai has a listing per lead and listingUrl is filled
STARTUP_BUCKETS = ['product_hunt', 'product_hunt_archive', 'product_hunt_under_30', 'launch_directories', 'shopify_app_store',
                   'browser_extensions', 'google_play', 'startups_hiring', 'yc', 'bangalore_startup_map', 'other']
# product -> Instantly campaign name, or (founder, shared inbox) where the copy is still split by audience
CAMPAIGN = {
    'backlinkbot_launch': 'BacklinkBot Launch - Claim listing',
    'backlinkbot_dfy':    ('BacklinkBot - Launch + tool - Founder', 'BacklinkBot - Launch + tool - Team inbox'),
    'backlinkbot':        ('BacklinkBot - Agent - Founder', 'BacklinkBot - Agent - Team inbox'),
    'xautodm':            ('XAutoDM - Startups - Founder', 'XAutoDM - Startups - Team inbox'),
    'chatterapi':         'ChatterAPI - Builders + founders',
    'hearbuyers':         'HearBuyers - Founders (one campaign)',
    'subredditmarketing': 'Subreddit Marketing - B2B SaaS',
    'hookads':            'HookAds',
    'hearbuyers_agencies': 'HearBuyers - Agencies',
    'yelp_ads':           'Yelp - AI ads',
    'yelp_local':         'Yelp - Local listings + SEO',
}
PRIORITY_DR, PRIORITY_AGE_DAYS = 20, 90       # Piyush 2026-10-20: older startups with DR > 20 go first, every campaign
FRESH_PRODUCTS = {'backlinkbot', 'backlinkbot_dfy', 'backlinkbot_launch'}   # time-sensitive: launch pitch
FAMILY = {'backlinkbot_launch': 'backlinkbot', 'backlinkbot_dfy': 'backlinkbot', 'hearbuyers_agencies': 'hearbuyers'}
TRACTION_LISTS = {'funded', 'hiring', 'revenue'}
INBOX_ORDER = ['founders', 'founder', 'owner', 'hi', 'hello', 'hey', 'team', 'contact', 'office', 'partnerships', 'marketing',
               'growth', 'info', 'sales']
BUCKET_LINE = {'product_hunt': 'your Product Hunt launch', 'product_hunt_archive': 'your Product Hunt launch',
               'product_hunt_under_30': 'your Product Hunt launch', 'shopify_app_store': 'your Shopify app',
               'browser_extensions': 'your Chrome extension', 'google_play': 'your app on Google Play',
               'startups_hiring': 'that you are hiring', 'yc': '{c} in the YC directory',
               'bangalore_startup_map': '{c} on the Bangalore startup map'}
# Yelp search -> the trade, read as "one new roofing customer", "other roofing businesses", "roofing services"
TRADE = {'Roofers': 'roofing', 'HVAC': 'HVAC', 'Chiropractors': 'chiropractic', 'Med Spas': 'med spa',
         'Remodeling Contractors': 'remodeling', 'Plumbers': 'plumbing', 'Landscapers': 'landscaping',
         'Personal Injury Lawyers': 'personal injury', 'Electricians': 'electrical', 'Auto Repair': 'auto repair',
         'Home Cleaning': 'cleaning', 'Gyms': 'fitness', 'Real Estate Agents': 'real estate', 'Pool Services': 'pool',
         'Solar Installers': 'solar', 'Movers': 'moving', 'Pest Control': 'pest control', 'Garage Door Services': 'garage door',
         'Dentists': 'dental', 'Physical Therapy': 'physical therapy', 'Cosmetic Dentists': 'cosmetic dental'}
# mailboxes nobody should pitch, and shared inboxes that get the "pass this on" line instead of being treated as a person
NEVER = {'privacy', 'legal', 'dpo', 'gdpr', 'compliance', 'security', 'abuse', 'billing', 'invoices', 'accounts', 'accounting',
         'payments', 'finance', 'careers', 'jobs', 'hr', 'recruiting', 'press', 'media', 'pr', 'noreply', 'no-reply', 'donotreply',
         'support', 'help', 'helpdesk', 'cs', 'customercare', 'customerservice', 'customer.service', 'clientservice', 'service',
         'feedback', 'bugs', 'webmaster', 'postmaster', 'admin', 'orders', 'returns', 'shipping', 'unsubscribe', 'investors', 'ir',
         'suporte', 'soporte', 'atendimento', 'sac', 'ayuda', 'hilfe', 'datenschutz', 'impressum'}
SHARED = {'hello', 'hi', 'hey', 'info', 'contact', 'team', 'founders', 'founder', 'office', 'connect', 'partnerships', 'partners',
          'marketing', 'growth', 'sales', 'business', 'enquiries', 'inquiries', 'mail', 'general', 'owner', 'ceo', 'start', 'inquiry',
          'contato', 'contacto', 'kontakt', 'comercial', 'ecomercial', 'vendas', 'ventas'}
SKIP_DOMAINS = {'qq.com', '163.com', '126.com', 'yeah.net', 'sina.com', 'example.com'}
ENGAGED = "coalesce(l.best_bucket, 'Not contacted') not in ('Hot', 'Replied', 'Neutral', 'Bounced', 'Unsubscribed')"


def call(path, body=None):
    req = urllib.request.Request(API + path, json.dumps(body, default=str).encode() if body is not None else None,
                                 {'Authorization': f"Bearer {ENV['INSTANTLY_API_KEY']}", 'Content-Type': 'application/json',
                                  'User-Agent': 'pp-daily-sync/1.0'})
    return json.load(urllib.request.urlopen(req, timeout=120))


# ---- candidates per segment ---------------------------------------------------------------------------
def startups():
    return DB.execute(f"""
      select 'startup' as seg, l.email, l.first_name, l.last_name, l.company, l.website, l.contact_type, l.bucket,
             l.verified, l.reoon_safe, l.reoon_status, l.reoon_checked_at as verified_on,
             c.domain, c.name as company_name, c.domain_rating as dr, c.business_type, c.category, c.lists,
             coalesce(l.ph_launch_date, (c.signals->>'ph_day')::date,          -- real launch dates only: the import date of an
                      to_date(c.signals->>'mdb_week', 'IYYY-"W"IW')) as launched   -- old list is not a launch -> back catalog
      from leads l join companies c on c.domain = l.company_domain
      where l.bucket = any(%s) and l.verified in ('yes', 'risky') and {ENGAGED}
        and not exists (select 1 from suppression s where s.email = l.email)""", (STARTUP_BUCKETS,)).fetchall()


def agencies():
    return DB.execute(f"""
      select 'agency' as seg, l.email, l.first_name, l.last_name, l.company, l.website, l.contact_type, l.bucket,
             l.verified, l.reoon_safe, l.reoon_status, l.reoon_checked_at as verified_on,
             coalesce(l.company_domain, split_part(l.email, '@', 2)) as domain, l.company as company_name
      from leads l where l.bucket = 'us_agency_owners_2025' and l.verified in ('yes', 'risky') and {ENGAGED}
        and not exists (select 1 from suppression s where s.email = l.email)""").fetchall()


def yelp():
    rows = DB.execute(f"""
      select distinct on (l.email) 'yelp' as seg, l.email, l.first_name, l.last_name, y.website, l.contact_type, 'yelp' as bucket,
             l.verified, l.reoon_safe, l.reoon_status, l.reoon_checked_at as verified_on,
             coalesce(y.domain, y.yelp_id) as domain, y.name as company_name, y.query, y.rating, y.review_count,
             coalesce(nullif(y.city, ''), split_part(y.location, ',', 1)) as city,
             y.is_advertiser or coalesce(y.call_tracking, false) as advertiser
      from yelp_businesses y join leads l on l.email = any(y.emails)
      where not y.is_closed and coalesce(y.wa_status, 'to send') <> 'skip' and l.verified in ('yes', 'risky') and {ENGAGED}
        and not exists (select 1 from suppression s where s.email = l.email)
      order by l.email, y.review_count desc nulls last""").fetchall()
    for r in rows:   # long Yelp names carry a tagline after " - "
        r['company'] = r['company_name'] = r['company_name'].split(' - ')[0].strip() if len(r['company_name']) > 28 else r['company_name']
    return rows


# ---- product choice --------------------------------------------------------------------------------------
def fits(r):
    """Products that suit a startup, as tiers: the first tier is split evenly, the rest is the ladder."""
    bt, cat, lists = r['business_type'] or '', r['category'] or '', set(r['lists'] or [])
    traction = bool(TRACTION_LISTS & lists) or r['bucket'] in ('yc', 'startups_hiring') or (r['dr'] or 0) >= 30
    if bt == 'Developer tool / API' or cat in ('AI agents & dev infrastructure', 'Developer tools'):
        return [['chatterapi'], 'xautodm', 'hearbuyers']
    if r['bucket'] != 'shopify_app_store' and (bt in ('B2C app', 'E-commerce / physical product') or cat == 'E-commerce & retail'
                                               or r['bucket'] == 'google_play'):
        return [['hookads'], 'xautodm']                  # Shopify app makers sell to merchants: they stay B2B
    if bt in ('Agency / services', 'Media / community', 'Marketplace / directory'):
        return [['xautodm', 'hearbuyers']]
    return [['xautodm', 'hearbuyers'] + (['subredditmarketing'] if traction else [])]   # B2B SaaS and unclassified


def pick(r, done, today):
    """(product, reason) or (None, why not)."""
    if r['seg'] == 'agency':
        return ('hearbuyers_agencies', 'agency owner') if 'hearbuyers' not in done else (None, 'ladder finished')
    if r['seg'] == 'yelp':
        if 'yelp' in done: return None, 'ladder finished'
        if r['query'] not in TRADE: return None, 'Yelp category without a trade word'
        if r['advertiser']: return 'yelp_ads', 'advertises on Yelp'
        if r['rating'] and r['review_count']: return 'yelp_local', f"{r['review_count']} reviews"
        return None, 'Yelp listing without reviews'
    fresh = r['launched'] and (today - r['launched']).days <= FRESH_DAYS
    if 'backlinkbot' not in done and fresh:
        if r['dr'] is None: return None, 'no DR yet'
        if r['dr'] >= 30: return 'backlinkbot', f"fresh, DR {r['dr']}"
        return ('backlinkbot_launch' if LAUNCH_CLAIM_READY else 'backlinkbot_dfy'), f"fresh, DR {r['dr']} < 30"
    tiers = fits(r)
    first = [p for p in tiers[0] if p not in done]
    if first:
        p = first[int(hashlib.md5(r['email'].encode()).hexdigest(), 16) % len(first)]
        return p, f"{'after BacklinkBot' if fresh else 'back catalog'}, {r['business_type'] or 'unclassified'}"
    for p in tiers[1:] + ['backlinkbot']:
        if p not in done: return p, 'next on ladder'
    return None, 'ladder finished'


def variables(r):
    first = (r['first_name'] or '').strip()
    local = r['email'].split('@')[0].lower()
    if first.lower() in ('', 'there', 'hi', 'hello', 'team', 'admin', 'info', 'office') or any(ch.isdigit() for ch in first) \
            or (first.lower() == local and len(first) > 10):          # "Podcaststotext" is a mailbox, not a name
        first = None
    company = r['company_name'] or r['company'] or r['domain']
    v = {'greeting': first or ('there' if r['contact_type'] not in ('company', None) else f'{company} team'),
         'inboxLine': (" I couldn't find the founder's email, so I'm writing here. Could you pass this to whoever runs growth?"
                       if r['contact_type'] == 'company' else None)}
    if r['seg'] == 'startup':
        src = next((l for l in ('funded', 'hiring', 'revenue', 'producthunt', 'showhn', 'launches', 'chrome', 'firefox', 'shopify', 'play')
                    if l in (r['lists'] or [])), None)
        v['sourceLine'] = SOURCE_LINE[src] if src else BUCKET_LINE.get(r['bucket'], 'your launch').format(c=company)
        v['domainRating'] = r['dr']
    if r['seg'] == 'yelp':
        v.pop('inboxLine')
        rating = float(r['rating'] or 0)
        v.update({'niche': TRADE.get(r['query']), 'city': r['city'], 'reviews': r['review_count'],
                  'rating': (str(int(rating)) if rating == int(rating) else str(rating)) if rating else None})
    v.update({'reoonVerified': 'yes' if r.get('reoon_safe') else 'no', 'reoonStatus': r.get('reoon_status'),
              'reoonCheckedAt': r['verified_on'].date().isoformat() if r.get('verified_on') else None})
    return {k: x for k, x in v.items() if x not in (None, '')}


# ---- run ---------------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--products', help='comma list: only fill these products')
    ap.add_argument('--weeks', type=float, default=TARGET_WEEKS, help='top each campaign up to this many weeks of new leads')
    ap.add_argument('--show', type=int, default=0)
    ap.add_argument('--all-priority', action='store_true',
                    help='push every priority lead (back catalog, launched 90+ days ago or undated, DR > PRIORITY_DR) past the top-up cap')
    a = ap.parse_args()
    only = set(a.products.split(',')) if a.products else None
    today, now = dt.date.today(), dt.datetime.now(dt.timezone.utc)

    hist = collections.defaultdict(list)
    for o in DB.execute("select email, product, outcome, campaign_id, coalesce(last_contact_at, added_at, synced_at) as at from outreach").fetchall():
        hist[o['email']].append(o)
    rows = startups() + agencies() + yelp()
    why = collections.Counter()
    busy = {r['domain'] for r in rows for o in hist.get(r['email'], [])
            if o['outcome'] in ('queued', 'in_sequence', 'interested', 'replied', 'ooo')}
    rank = lambda r: (not r['reoon_safe'], r['contact_type'] == 'company',   # a sendable address first, then founder over inbox
                      next((i for i, w in enumerate(INBOX_ORDER) if r['email'].startswith(w + '@')), len(INBOX_ORDER)))
    per_co = {}
    for r in rows:
        local, dom = r['email'].split('@', 1)
        base = local.lower().split('+')[0]
        r['_blocked'] = base in NEVER or dom in SKIP_DOMAINS
        if base in SHARED: r['contact_type'] = 'company'                # shared inbox, whatever the import said
    rows = [r for r in rows if not r['_blocked'] or why.update(['support / privacy / role mailbox'])]
    for r in sorted(rows, key=rank):                                                                          # R6
        if r['domain'] in busy: why['company already in a conversation'] += 1; continue
        per_co.setdefault((r['seg'], r['domain']), r)
    plan = collections.defaultdict(list)
    for r in per_co.values():
        h = hist.get(r['email'], [])
        if any(o['outcome'] in ('interested', 'replied') for o in h): why['replied before'] += 1; continue      # R7
        if h and (now - max(o['at'] for o in h)).days < GAP_DAYS: why[f'emailed in the last {GAP_DAYS} days'] += 1; continue  # R3
        if len({FAMILY.get(o['product'], o['product']) for o in h if (now - o['at']).days <= 180}) >= MAX_PRODUCTS_180D:
            why['3 products in 180 days'] += 1; continue                                                    # R5
        if not r['reoon_safe']: why[f"not Reoon-verified ({r['reoon_status'] or 'never checked'})"] += 1; continue  # R8
        if not r['verified_on'] or (now - r['verified_on']).days > VERIFY_DAYS:
            why[f'Reoon check over {VERIFY_DAYS} days old'] += 1; continue
        done = {FAMILY.get(o['product'], o['product']) for o in h} | ({'yelp'} if any(o['product'].startswith('yelp') for o in h) else set())
        p, reason = pick(r, done, today)
        if not p: why[reason] += 1; continue
        r['_reason'], r['_product'] = reason, p
        plan[p].append(r)

    def priority(r):
        old = r.get('launched') is None or (today - r['launched']).days >= PRIORITY_AGE_DAYS
        return r['seg'] == 'startup' and old and (r.get('dr') or 0) > PRIORITY_DR
    for p in plan:                                      # priority leads first, then the stronger domains
        plan[p].sort(key=lambda r: (not priority(r), -(r.get('dr') or 0)))

    camps = {c['name']: c for c in call('/campaigns?limit=100')['items']}
    queued = collections.Counter(o['campaign_id'] for hs in hist.values() for o in hs if o['outcome'] == 'queued')
    STATE = {0: 'draft', 1: 'active', 2: 'paused', 3: 'done'}
    print(f"{'product':20} {'due':>6} {'push':>5}  campaign [state, already queued]")
    pushes = []
    for p in sorted(plan, key=lambda p: -len(plan[p])):
        names = CAMPAIGN[p] if isinstance(CAMPAIGN[p], tuple) else (CAMPAIGN[p],)
        line = []
        for i, nm in enumerate(names):
            c = camps.get(nm)
            sel = [r for r in plan[p] if len(names) == 1 or (r['contact_type'] == 'company') == bool(i)]
            if not c: line.append(f'{nm} [MISSING]'); continue
            room = max(0, int(c.get('daily_limit') or 50) * 2 * a.weeks - queued[c['id']])
            if p in FRESH_PRODUCTS: room = len(sel)          # new launches go in the day they arrive: no top-up cap
            if a.all_priority: room = max(room, sum(1 for r in sel if priority(r)))
            take = sel[:int(room)] if (not only or p in only) else []
            pushes.append((p, c, take))
            line.append(f"{nm} [{STATE.get(c['status'], c['status'])}, {queued[c['id']]} queued] +{len(take)}")
        print(f"{p:20} {len(plan[p]):6} {sum(len(t) for q, _, t in pushes if q == p):5}  " + ' | '.join(line))
    print('\nnot routed:'); [print(f'  {k:40} {v}') for k, v in why.most_common()]
    if a.show:
        print('\nsample:')
        for p, c, take in pushes:
            for r in take[:max(1, a.show // max(1, len(pushes)))]:
                print(f"  {c['name'][:34]:34} {r['email']:38} {json.dumps(variables(r))[:140]}")
    if not a.apply:
        print('\ndry run, nothing sent. --apply pushes the "push" column.'); return

    for p, c, take in pushes:
        for i in range(0, len(take), 500):
            batch = [r for r in take[i:i + 500] if r.get('reoon_safe')]
            if not batch: continue
            leads = [{'email': r['email'], 'first_name': r['first_name'], 'last_name': r['last_name'],
                      'company_name': r['company_name'] or r['company'], 'website': r['website'],
                      'custom_variables': variables(r)} for r in batch]
            try:
                res = call('/leads/add', {'campaign_id': c['id'], 'skip_if_in_campaign': True, 'leads': leads})
            except urllib.error.HTTPError as e:
                raise SystemExit(f"{c['name']}: Instantly HTTP {e.code} {e.read()[:300]!r}; nothing recorded for this batch")
            with DB.conn.transaction():
                for r in batch:
                    DB.execute("""insert into outreach (email, campaign_id, campaign_name, product, outcome, added_at, dr_at_push, route_reason)
                                  values (%s, %s, %s, %s, 'queued', now(), %s, %s) on conflict do nothing""",
                               (r['email'], c['id'], c['name'], p, r.get('dr'), r['_reason']))
            print(f"{c['name']}: +{res.get('leads_uploaded')} (skipped {res.get('skipped_count')}, invalid {res.get('invalid_email_count')})")


if __name__ == '__main__':
    main()
