"""Enrich companies in Neon: DR (DataForSEO bulk ranks, 0-100), homepage title/description, and category.

    python enrich_companies.py                     # all three steps for 2026 startup companies still missing data
    python enrich_companies.py --step dr --refresh-days 90   # also re-check DR older than 90 days
    python enrich_companies.py --limit 200         # try it on a few first

Scope: companies behind a verified (yes/risky) 2026 lead in the startup buckets, plus every launch_directories lead.
Writes go through a staging table and only touch rows whose value changed (Neon free plan is 512 MB, see README).
Cost: DR ~$0.06 per 1,000 domains (DataForSEO bulk ranks); homepage fetch free; category on
the Azure chat deployment (startup credit).
"""
import argparse, base64, concurrent.futures as cf, csv, html, json, pathlib, re, subprocess, tempfile, urllib.request

from core import ENV
DB = ENV['DATABASE_URL']             # each write is one psql session wrapped in begin/commit, so the pooler is fine
DFS = ENV['DATAFORSEO_AUTH_BASE64']  # DataForSEO account amplifyxlabs@gmail.com
AZ_KEY = ENV['AZURE_CHAT_KEY']
AZ_URL = 'https://squire-test.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36'
STARTUP_BUCKETS = ('product_hunt', 'product_hunt_archive', 'product_hunt_under_30', 'launch_directories', 'shopify_app_store',
                   'browser_extensions', 'google_play', 'startups_hiring', 'yc', 'bangalore_startup_map', 'other')
CATS = ['AI agents & dev infrastructure', 'Developer tools', 'SEO & AI search visibility', 'Marketing & advertising',
        'Sales & lead generation', 'Social media & creator tools', 'Writing & content', 'Design & creative',
        'AI image/video/audio generation', 'Productivity & personal tools', 'Business, finance & legal',
        'Hiring & careers', 'Education & learning', 'Health, fitness & wellness', 'E-commerce & retail',
        'Travel, local & lifestyle', 'Real estate', 'Games & entertainment', 'Security & privacy',
        'Crypto & web3', 'Media & news', 'Directories & launch platforms', 'Agency & services', 'Other']
TYPES = ['B2B SaaS', 'B2C app', 'Developer tool / API', 'Agency / services', 'E-commerce / physical product',
         'Marketplace / directory', 'Media / community', 'Other']

ap = argparse.ArgumentParser()
ap.add_argument('--step', choices=['dr', 'sites', 'classify', 'all'], default='all')
ap.add_argument("--refresh-days", type=int, default=90, help="also re-fetch DR checked longer ago than this")
ap.add_argument('--limit', type=int)
a = ap.parse_args()


def psql(sql, *flags):
    r = subprocess.run(['psql', DB, '-v', 'ON_ERROR_STOP=1', '-q', '-At', *flags, '-c', sql], capture_output=True, text=True)
    if r.returncode: raise SystemExit(r.stderr)
    return r.stdout.strip()


psql("""alter table companies add column if not exists dr_checked_at timestamptz;
        alter table companies add column if not exists site_desc text;
        alter table companies add column if not exists classified_at timestamptz""")

SCOPE = f"""select distinct l.company_domain from leads l where l.company_domain is not null
  and l.verified in ('yes', 'risky') and l.bucket in {STARTUP_BUCKETS}
  and (l.bucket = 'launch_directories' or coalesce(l.ph_launch_date, l.first_added::date, l.created_at::date) >= '2026-01-01')"""


def todo(cond):
    clean = lambda x: f"regexp_replace(coalesce({x}, ''), '\\s+', ' ', 'g')"     # a tab or newline in a title would split the row
    tag = clean("c.signals->>'tagline'")
    rows = psql(f"select c.domain, {clean('c.site_title')}, {clean('c.site_desc')}, {tag}, "
                f"{clean('c.name')} from companies c where c.domain in ({SCOPE}) and ({cond}) order by c.domain"
                + (f' limit {a.limit}' if a.limit else ''), '-F', '\t').splitlines()
    return [r.split('\t') for r in rows if r.count('\t') == 4]


def write(cols, rows):
    """Stage rows (domain + cols) and update only companies whose values differ."""
    if not rows: return 0
    f = pathlib.Path(tempfile.mkdtemp()) / 'u.csv'
    with open(f, 'w', newline='') as fh: csv.writer(fh).writerows(rows)
    types = {'domain_rating': 'int', 'site_reachable': 'boolean', 'dr_checked_at': 'timestamptz', 'classified_at': 'timestamptz'}
    sets = ', '.join(f'{c} = u.{c}' for c in cols)
    diff = ' or '.join(f'c.{c} is distinct from u.{c}' for c in cols if not c.endswith('_at'))
    out = subprocess.run(['psql', DB, '-v', 'ON_ERROR_STOP=1', '-q', '-At'], input=f"""
        begin;
        create temp table u (domain text, {', '.join(f'{c} {types.get(c, 'text')}' for c in cols)}) on commit drop;
        \\copy u from '{f}' with (format csv)
        with x as (update companies c set {sets}, updated_at = now() from u where u.domain = c.domain and ({diff}) returning 1)
        select count(*) from x;
        commit;""", capture_output=True, text=True)
    if out.returncode: raise SystemExit(out.stderr)
    return int(out.stdout.split()[-1])


# ---- DR ------------------------------------------------------------------------------------------
if a.step in ('dr', 'all'):
    cond = 'c.dr_checked_at is null' + (f" or c.dr_checked_at < now() - interval '{a.refresh_days} days'" if a.refresh_days else '')
    doms = [r[0] for r in todo(cond)]
    print(f'DR: {len(doms)} domains to fetch')
    cost, changed = 0.0, 0
    for i in range(0, len(doms), 1000):
        batch = doms[i:i + 1000]
        req = urllib.request.Request('https://api.dataforseo.com/v3/backlinks/bulk_ranks/live',
                                     json.dumps([{'targets': batch, 'rank_scale': 'one_hundred'}]).encode(),
                                     {'Authorization': 'Basic ' + DFS, 'Content-Type': 'application/json'})
        t = json.load(urllib.request.urlopen(req, timeout=180))['tasks'][0]
        if t['status_code'] != 20000: raise SystemExit(f"DR batch {i}: {t['status_code']} {t['status_message']}")
        rank = {it['target']: it['rank'] for it in (t['result'][0]['items'] or [])}
        now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
        # a domain DataForSEO has never seen comes back without a rank: that is DR 0, not unknown
        changed += write(['domain_rating', 'dr_checked_at'], [(d, rank.get(d) or 0, now) for d in batch])
        psql(f"update companies set dr_checked_at = now() where domain in ('" + "','".join(d.replace("'", "''") for d in batch) + "') and (dr_checked_at is null or dr_checked_at < now() - interval '1 day')")
        cost += t['cost']
        print(f'  batch {i // 1000 + 1}: {len(batch)} domains, ${t["cost"]:.3f}', flush=True)
    print(f'DR done: {changed} changed, ${cost:.2f}')
    psql("update companies set dr_band = case when domain_rating < 30 then 'DR <30' else 'DR 30+' end "
         "where domain_rating is not null and dr_band is distinct from case when domain_rating < 30 then 'DR <30' else 'DR 30+' end")


# ---- homepage text ---------------------------------------------------------------------------------
def fetch(d):
    for url in (f'https://{d}', f'https://www.{d}'):
        try:
            raw = urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': UA}), timeout=10).read(300_000).decode('utf-8', 'ignore')
        except Exception:
            continue
        t = re.search(r'<title[^>]*>(.*?)</title>', raw, re.S | re.I)
        m = re.search(r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]*content=["\']([^"\']*)', raw, re.I) or \
            re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:name|property)=["\'](?:og:)?description', raw, re.I)
        clean = lambda s: re.sub(r'\s+', ' ', html.unescape(s or '')).strip()[:300]
        return d, clean(t.group(1) if t else ''), clean(m.group(1) if m else ''), 'true'
    return d, '', '', 'false'


if a.step in ('sites', 'all'):
    doms = [r[0] for r in todo("c.category is null and c.site_reachable is null and coalesce(c.site_title, '') = ''")]
    print(f'Sites: {len(doms)} homepages to fetch')
    with cf.ThreadPoolExecutor(48) as ex:
        rows = list(ex.map(fetch, doms))
    print(f'Sites done: {write(["site_title", "site_desc", "site_reachable"], rows)} changed, '
          f'{sum(r[3] == "false" for r in rows)} unreachable')


# ---- category ----------------------------------------------------------------------------------------
def classify(batch):
    items = [{'domain': d, 'name': n, 'title': t, 'description': ds, 'tagline': tg} for d, t, ds, tg, n in batch]
    prompt = ('Classify each company by what it sells. Use ONLY the given fields; if they are empty, infer from the '
              'domain name and set confidence "low". Return JSON only: {"results":[{"domain","category","business_type",'
              '"one_liner","confidence"}]} with one entry per input domain.\n'
              f'category must be one of: {json.dumps(CATS)}\nbusiness_type must be one of: {json.dumps(TYPES)}\n'
              'one_liner: max 12 words, plain, what it does. confidence: high|medium|low.\n\n' + json.dumps(items))
    body = {'model': 'gpt-chat-latest', 'input': [{'role': 'user', 'content': prompt}], 'max_output_tokens': 20000}
    err = None
    for _ in range(3):
        try:
            r = json.load(urllib.request.urlopen(urllib.request.Request(AZ_URL, json.dumps(body).encode(),
                {'Authorization': 'Bearer ' + AZ_KEY, 'Content-Type': 'application/json'}), timeout=240))
            txt = ''.join(c.get('text', '') for o in r.get('output', []) for c in (o.get('content') or []))
            res = json.loads(txt[txt.index('{'):txt.rindex('}') + 1])['results']
            return [(x['domain'], x.get('category') if x.get('category') in CATS else 'Other',
                     x.get('business_type') if x.get('business_type') in TYPES else 'Other',
                     x.get('one_liner'), x.get('confidence'), 'now()') for x in res
                    if x.get('domain') in {b[0] for b in batch}]
        except Exception as e:
            err = e
    print('  batch failed:', batch[0][0], err); return []


if a.step in ('classify', 'all'):
    rows = todo('c.category is null')
    batches = [rows[i:i + 25] for i in range(0, len(rows), 25)]
    print(f'Classify: {len(rows)} companies in {len(batches)} calls')
    done = 0
    with cf.ThreadPoolExecutor(10) as ex:
        buf = []
        for n, res in enumerate(ex.map(classify, batches), 1):
            buf += res
            if len(buf) >= 1000 or n == len(batches):
                done += write(['category', 'business_type', 'what_they_do', 'category_confidence', 'classified_at'], buf); buf = []
                print(f'  {n}/{len(batches)} calls, {done} written', flush=True)
    print(f'Classify done: {done} companies')
