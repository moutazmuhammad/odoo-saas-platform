#!/usr/bin/env python3
"""Validate and export the canonical bilingual customer documentation.

Usage: python3 scripts/generate-customer-docs.py [--check]
The catalog is the only editorial source. Do not edit generated Markdown/help.
"""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'frontend/veltnex/src/lib/docs-catalog.json'
HELP_MAP = {
 'workers':'sizing','storage':'storage-capacity','region':'region-version','odoo-version':'region-version',
 'billing-period':'billing-options','yearly-discount':'billing-options','subdomain':'access-urls','repo':'custom-code',
 'daily-backup':'daily-backups','support-plan':'support-plans','trial':'free-trial',
 'proration':'optional-charges','invoice-status':'invoices','decline-invoice':'optional-charges','change-plan':'change-plan',
 'reactivate':'reactivate','snapshots':'daily-backups','restore':'restore','cpu-usage':'monitoring','ram-usage':'monitoring',
 'storage-usage':'storage-capacity','logs':'application-logs','create-database':'manage-databases',
}
BRANDS = re.compile(r'\b(?:VELTNEX|Veltnex|Odoo|Kubernetes|GitHub|GitLab|Gitea|Bitbucket|AWS|Docker|PostgreSQL|Python|WhatsApp|Community|Enterprise|Standard|CPU|RAM|HTTPS|SQL|API|CORS|ZIP|Production|Staging|Development|Shell|Container|Terminal|Workers|Domain)\b')

def localized_values(value):
 if isinstance(value, dict):
  if set(value) == {'en','ar'}: yield value
  else:
   for item in value.values(): yield from localized_values(item)
 elif isinstance(value, list):
  for item in value: yield from localized_values(item)

def validate(groups):
 articles = [a for g in groups for a in g['articles']]
 ids = {a['id'] for a in articles}
 assert len(ids) == len(articles), 'Duplicate article IDs'
 assert len({g['id'] for g in groups}) == len(groups), 'Duplicate category IDs'
 for a in articles:
  assert a['related'] and a['sources'] and a['sections'], a['id']
  assert len({s['id'] for s in a['sections']}) == len(a['sections']), a['id']
  assert set(a['related']) <= ids and a['id'] not in a['related'], a['id']
  for source in a['sources']: assert (ROOT / source).exists(), (a['id'], source)
  for section in a['sections']:
   assert re.fullmatch(r'[a-z0-9-]+', section['id']), section['id']
   for block in section['blocks']:
    assert block['type'] in ('paragraph','note','code','list','table'), block
    if block['type']=='table': assert all(len(row)==len(block['headers']) for row in block['rows']), a['id']
    if block['type']=='code': assert block['code'].strip(), a['id']
  for pair in localized_values(a):
   assert pair['en'].strip() and pair['ar'].strip(), a['id']
   for token in BRANDS.findall(pair['en']): assert token in pair['ar'], (a['id'], token, pair)
   assert re.findall(r'`([^`]+)`',pair['en']) == re.findall(r'`([^`]+)`',pair['ar']), (a['id'],'code',pair)
   assert re.findall(r'\]\(([^)]+)\)',pair['en']) == re.findall(r'\]\(([^)]+)\)',pair['ar']), (a['id'],'links',pair)
   for n in re.findall(r'\b\d+(?:\.\d+)?\b',pair['en']): assert n in pair['ar'], (a['id'],n)
   for target in re.findall(r'\]\(/docs/([a-z0-9-]+)\)',pair['en']): assert target in ids, target
 return articles

def md_block(block, lang):
 text=lambda t:t[lang]
 if block['type']=='paragraph': return text(block['text'])
 if block['type']=='note': return '> '+('Warning: ' if lang=='en' and block['tone']=='warning' else 'تنبيه: ' if lang=='ar' and block['tone']=='warning' else '')+text(block['text'])
 if block['type']=='code': return f"```{block['language']}\n{block['code']}\n```"
 if block['type']=='list': return '\n'.join(f'{i+1}. {text(t)}' if block['ordered'] else '- '+text(t) for i,t in enumerate(block['items']))
 cells=lambda row:'| '+' | '.join(text(t).replace('|','\\|') for t in row)+' |'
 return '\n'.join([cells(block['headers']),'| '+' | '.join('---' for _ in block['headers'])+' |',*[cells(row) for row in block['rows']]])

def outputs(groups, articles):
 by_id={a['id']:a for a in articles}
 generated={}
 help_topics=[]
 for anchor,id in HELP_MAP.items():
  a=by_id[id];g=next(g for g in groups if a in g['articles'])
  paragraph=next((b['text'] for s in a['sections'] for b in s['blocks'] if b['type']=='paragraph'),a['summary'])
  help_topics.append(dict(anchor=anchor,article=id,category=g['title'],title=a['title'],tip=a['summary'],body=[paragraph]))
 help_bytes=(json.dumps(help_topics,ensure_ascii=False,indent=2)+'\n').encode()
 generated['frontend/veltnex/src/lib/docs-help.json']=help_bytes
 generated['control-plane/saas_website/static/src/docs/help.json']=help_bytes
 for lang in ('en','ar'):
  index=['# VELTNEX '+('customer documentation' if lang=='en' else 'وثائق العملاء'),'','2026-10-05','']
  for g in groups:
   index += ['## '+g['title'][lang],'',g['description'][lang],'']
   for a in g['articles']:
    index += [f"- [{a['title'][lang]}]({a['id']}.md)"]
    body=['# '+a['title'][lang],'',a['summary'][lang],'',('Reviewed: ' if lang=='en' else 'تاريخ المراجعة: ')+a['reviewed'],'']
    for section in a['sections']:
     body += ['## '+section['title'][lang],'']
     for b in section['blocks']: body += [md_block(b,lang),'']
    body += ['## '+('Related guides' if lang=='en' else 'أدلة ذات صلة'),'']
    body += [f"- [{by_id[id]['title'][lang]}]({id}.md)" for id in a['related']]
    md='\n'.join(body)+'\n'
    md=re.sub(r'\]\(/docs/([a-z0-9-]+)\)',r'](\1.md)',md)
    generated[f'docs/customer/{lang}/{a["id"]}.md']=md.encode()
   index += ['']
  generated[f'docs/customer/{lang}/README.md']=('\n'.join(index).rstrip()+'\n').encode()
 review=['# Customer feature implementation review', '', 'Reviewed on 2026-10-05. Articles describe the customer-facing implementation and distinguish defaults from configurable behavior. Live provider delivery and payment success remain configuration-dependent.', '', '| Category | Customer article | Implementation sources |', '| --- | --- | --- |']
 for group in groups:
  for article in group['articles']:
   sources=', '.join(f'[{source}](../../{source})' for source in article['sources'])
   review.append(f"| {group['title']['en']} | [{article['title']['en']}](en/{article['id']}.md) | {sources} |")
 generated['docs/customer/IMPLEMENTATION-REVIEW.md']=('\n'.join(review)+'\n').encode()
 return generated

def main():
 check=argparse.ArgumentParser();check.add_argument('--check',action='store_true');args=check.parse_args()
 groups=json.loads(CATALOG.read_text());articles=validate(groups)
 for relative,content in outputs(groups,articles).items():
  path=ROOT/relative
  if args.check: assert path.exists() and path.read_bytes()==content, f'Regenerate {relative}'
  else: path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
 print(f'Validated {len(groups)} categories, {len(articles)} bilingual articles, sources, links, identifiers and exports.')
if __name__=='__main__':main()
