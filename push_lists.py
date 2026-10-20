"""Export step."""
import argparse, collections, json, urllib.error, urllib.request
from core import DB, ENV

LIST_NAME = {'producthunt': 'PH', 'launches': 'Launches', 'revenue': 'Revenue', 'funded': 'Funded', 'chrome': 'Chrome', 'shopify': 'Shopify',
             'showhn': 'Show HN', 'stores': 'Stores', 'hiring': 'Hiring', 'firefox': 'Firefox', 'play': 'Google Play'}
PRODUCT_NAME = {'backlinkbot': 'BacklinkBot', 'hookads': 'HookAds', 'xautodm': 'XAutoDM', 'subredditmarketing': 'Subreddit Marketing',
                'growwithreddit': 'GrowWithReddit', 'phlaunchkit': 'PH LaunchKit', 'tokgency': 'TokGency UGC'}
PRIORITY = {  # per source list: which product to pitch first
    'producthunt': ['backlinkbot', 'hookads', 'subredditmarketing', 'xautodm'],
    'launches':    ['backlinkbot', 'phlaunchkit', 'hookads', 'xautodm'],
    'revenue':     ['subredditmarketing', 'growwithreddit', 'xautodm', 'backlinkbot'],
    'funded':      ['subredditmarketing', 'tokgency', 'xautodm', 'backlinkbot'],
    'chrome':      ['backlinkbot', 'phlaunchkit', 'xautodm'],
    'shopify':     ['hookads', 'backlinkbot'],
    'showhn':      ['backlinkbot', 'phlaunchkit', 'xautodm'],
    'stores':      ['hookads', 'tokgency'],
    'hiring':      ['subredditmarketing', 'growwithreddit', 'xautodm', 'backlinkbot'],
    'firefox':     ['growwithreddit', 'backlinkbot', 'xautodm'],
    'play':        ['hookads', 'backlinkbot'],
}
STAGE = {'producthunt': 'Launched', 'launches': 'Launched', 'chrome': 'Launched', 'shopify': 'Launched',
         'revenue': 'Revenue', 'funded': 'Funded', 'showhn': 'Launched', 'firefox': 'Launched', 'play': 'Apps',
         'stores': 'Stores', 'hiring': 'Hiring'}   # one campaign per product x audience x stage; copy differs by stage
SOURCE_LINE = {'producthunt': 'your Product Hunt launch', 'launches': 'your launch', 'chrome': 'your Chrome extension',
               'shopify': 'your Shopify app', 'revenue': 'your numbers on TrustMRR', 'funded': 'the YC batch',
               'showhn': 'your Show HN post', 'stores': 'your store', 'hiring': 'that you are hiring', 'firefox': 'your Firefox add-on',
               'play': 'your app on Google Play'}
INBOX_ORDER = ['founders', 'founder', 'hi', 'hello', 'hey', 'team', 'contact', 'partnerships', 'marketing', 'growth', 'info', 'sales']
NEXT_PRODUCT_DAYS = 35
API = 'https://api.instantly.ai/api/v2'


def call(path, body=None, method=None):
    req = urllib.request.Request(API + path, json.dumps(body).encode() if body is not None else None,
                                 {'Authorization': f"Bearer {ENV['INSTANTLY_API_KEY']}", 'Content-Type': 'application/json',
                                  'User-Agent': 'pp-daily-sync/1.0'}, method=method)
    return json.load(urllib.request.urlopen(req, timeout=60))


def due(catch_all=False):
    return DB.execute("""
      select l.email, l.first_name, l.last_name, l.company, l.website, l.email_status, l.contact_type, l.reoon_status, l.email_origin, l.fit_products, l.x_handle,
             l.linkedin, l.title, l.buckets, c.domain, c.lists, c.domain_rating, c.signals, c.name as company_name,
             (select array_agg(p.product) from instantly_pushes p where p.email = l.email) as pushed,
             (select max(p.pushed_at) from instantly_pushes p where p.email = l.email) as last_push
      from leads l join companies c on c.domain = l.company_domain
      where cardinality(c.lists) > 0 and cardinality(l.fit_products) > 0
        and l.reoon_checked_at > now() - interval '60 days'
        and l.reoon_safe and %(catch_all)s is not null   -- HARD RULE 2026-10-20: Reoon-safe only, catch-alls never
        and not exists (select 1 from suppression s where s.email = l.email)
        and coalesce(l.best_bucket, 'Not contacted') not in ('Hot', 'Replied', 'Neutral', 'Bounced', 'Unsubscribed')""",
        {'catch_all': catch_all}).fetchall()


def plan(catch_all=False):
    out = collections.defaultdict(list)
    for r in due(catch_all):
        pushed = set(r['pushed'] or [])
        if pushed and r['last_push'] and (__import__('datetime').datetime.now(r['last_push'].tzinfo) - r['last_push']).days < NEXT_PRODUCT_DAYS:
            continue
        b = r['buckets'] or []
        # PH: over 30 upvotes, or under 30 collected from 2026-10-11 (own campaign); the older low-upvote archive is never pushed
        lists = [x for x in r['lists'] if not (x == 'producthunt' and not {'product_hunt', 'product_hunt_under_30'} & set(b))]
        src = next((l for l in ('funded', 'hiring', 'revenue', 'producthunt', 'showhn', 'launches', 'chrome', 'firefox', 'shopify', 'play', 'stores')
                    if l in lists), None)
        if not src: continue
        prod = next((p for p in PRIORITY[src] if p in r['fit_products'] and p not in pushed), None)
        if not prod: continue
        who = 'Inbox' if r['contact_type'] == 'company' else 'Founder'
        if r['reoon_status'] == 'catch_all':
            name = f"{PRODUCT_NAME[prod]} - Catch-all " + ('(published)' if r['email_origin'] == 'listed' else '(guessed, test)')
        else:
            stage = 'Launched under 30' if src == 'producthunt' and 'product_hunt' not in b else STAGE[src]
            name = f"{PRODUCT_NAME[prod]} - {who} - {stage}"
        r['_src'] = src
        out[(name, prod)].append(r)
    # a company with a founder mailbox is not also emailed at its shared inbox: one conversation per company
    has_founder = {r['domain'] for (n, _), rows in out.items() if ' - Founder - ' in n for r in rows}
    rank = lambda r: next((i for i, w in enumerate(INBOX_ORDER) if r['email'].startswith(w + '@')), len(INBOX_ORDER))
    for k in [k for k in out if ' - Inbox - ' in k[0]]:
        best = {}
        for r in sorted(out[k], key=rank):            # and only one shared inbox per company, the likeliest to reach a person
            if r['domain'] not in has_founder: best.setdefault(r['domain'], r)
        out[k] = list(best.values())
    return {k: v for k, v in out.items() if v}


def variables(r):
    import datetime
    sg = r['signals'] or {}
    company = r['company_name'] or r['company']
    first = (r['first_name'] or '').strip()
    first = first if first.lower() not in ('', 'there', 'hi', 'hello', 'team', 'admin', 'info') else None
    # "Hi {{greeting}},": the founder's name whenever we have it, shared inboxes included (team@ is read by the founder at
    # this size); a shared inbox with no known founder gets "<Company> team", a personal one "there"
    greeting = first or ('there' if r['contact_type'] != 'company' else f'{company} team')
    # one campaign serves founders and shared inboxes (2026-10-18): the shared inbox gets one extra sentence right
    # after "Saw {{sourceLine}}." in email 1; it starts with a space so a founder's copy has no gap
    inbox_line = (" I couldn't find the founder's email, so I'm writing here. Could you pass this to whoever runs growth?"
                  if r['contact_type'] == 'company' else None)
    v = {'greeting': greeting, 'maker': first, 'source': r['_src'], 'sourceLine': SOURCE_LINE[r['_src']], 'audience': r['contact_type'],
         'inboxLine': inbox_line,
         'launchMonth': datetime.date.fromisoformat(sg['ph_day']).strftime('%B') if sg.get('ph_day') else None,
         'upvotes': sg.get('upvotes'), 'tagline': sg.get('tagline'), 'mrr': sg.get('mrr'), 'ycBatch': sg.get('yc_batch'),
         'domainRating': r['domain_rating'], 'xHandle': r['x_handle'], 'title': r['title']}
    return {k: x for k, x in v.items() if x not in (None, '')}


def list_id(name, cache):
    if name in cache: return cache[name]
    lists, after = [], None
    while True:
        d = call('/lead-lists?limit=100' + (f'&starting_after={after}' if after else ''))
        lists += d.get('items', []); after = d.get('next_starting_after')
        if not after: break
    for l in lists: cache[l['name']] = l['id']
    if name not in cache:
        cache[name] = call('/lead-lists', {'name': name})['id']
    return cache[name]


if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('--apply', action='store_true')
    ap.add_argument('--include-guessed-catch-all', action='store_true'); a = ap.parse_args()
    p, cache = plan(a.include_guessed_catch_all), {}
    for (name, prod), rows in sorted(p.items()):
        print(f'{name:45} {len(rows):5}')
        if not a.apply: continue
        lid = list_id(name, cache)
        leads = [{'email': r['email'], 'first_name': r['first_name'], 'last_name': r['last_name'],
                  'company_name': r['company_name'] or r['company'], 'website': r['website'],
                  'custom_variables': variables(r)}
                 for r in rows]
        for i in range(0, len(leads), 500):
            try:
                call('/leads/add', {'list_id': lid, 'skip_if_in_workspace': True, 'leads': leads[i:i + 500]})
            except urllib.error.HTTPError as e:
                raise SystemExit(f'{name}: Instantly HTTP {e.code} {e.read()[:300]!r}; nothing recorded for this batch')
            for r in rows[i:i + 500]:
                DB.execute('insert into instantly_pushes (email, product, list_name, list_id) values (%s, %s, %s, %s) on conflict do nothing',
                           (r['email'], prod, name, lid))
    if not a.apply: print('\ndry run, nothing sent. --apply pushes to the primary Instantly workspace.')
