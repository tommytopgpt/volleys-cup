#!/usr/bin/env python3
"""Safely replace the current Volleys Cup standings with a completed ESPN week.

Uses only Python stdlib. Unofficial ESPN fantasy API may change.
"""
import argparse
import html
import json
import os
from pathlib import Path
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

LEAGUE_ID = 822887247
SEASON = 2026
DATA = Path('data/current.json')
ENDPOINTS = (
    'https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/{league}',
    'https://fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/{league}',
)


def clean(text):
    text = unicodedata.normalize(
        'NFKC', html.unescape(str(text or ''))
    )
    text = ''.join(
        ch if ch.isalnum() or ch.isspace() else ' '
        for ch in text
    )
    return ' '.join(text.casefold().split())


def espn_names(team):
    vals = [team.get('name'), ' '.join(filter(None, [team.get('location'), team.get('nickname')]))]
    return {clean(s) for s in vals if s and s.strip()}


def fetch():
    params = urllib.parse.urlencode([('view','mTeam'),('view','mSettings'),('view','mStatus')])
    cookie_bits = []
    for env_name, cookie_name in [('ESPN_S2','espn_s2'),('ESPN_SWID','SWID')]:
        value = os.getenv(env_name, '').strip()
        if value:
            cookie_bits.append(f'{cookie_name}={value}')
    headers = {'User-Agent':'Mozilla/5.0 (Volleys Cup standings updater)', 'Accept':'application/json'}
    if cookie_bits:
        headers['Cookie'] = '; '.join(cookie_bits)
    errors = []
    for url_tpl in ENDPOINTS:
        url = url_tpl.format(year=SEASON, league=LEAGUE_ID) + '?' + params
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                payload = json.load(response)
                if isinstance(payload, list):
                    payload = payload[0]
                if not isinstance(payload, dict) or not payload.get('teams'):
                    raise ValueError('Missing ESPN teams')
                return payload
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (401,403):
                raise RuntimeError('ESPN denied access (401/403). If this is a private league, add ESPN_S2 and ESPN_SWID as GitHub Actions secrets.') from None
            errors.append(f'{type(exc).__name__}: {getattr(exc,"code",str(exc))}')
    raise RuntimeError('Could not obtain valid ESPN league data. ' + '; '.join(errors))


def validate_and_update(espn, current, probe_only=False):
    existing = current.get('standings', [])
    teams = espn.get('teams', [])
    if len(existing) != 12 or len(teams) != 12:
        raise ValueError(f'Expected 12 teams; local={len(existing)}, ESPN={len(teams)}. Refusing update.')
    if int(espn.get('seasonId', SEASON)) != SEASON:
        raise ValueError('ESPN season mismatch')
    by_name = {}
    for old in existing:
        key = clean(old['team'])
        if key in by_name:
            raise ValueError('Duplicate existing team name')
        by_name[key] = old
    new_rows=[]
    match_log=[]
    for team in teams:
        matches = [by_name[k] for k in espn_names(team) if k in by_name]
        distinct = {x['gm_id']:x for x in matches}
        if len(distinct) != 1:
            raise ValueError(f'Cannot confidently match ESPN team {team.get("id")} {team.get("name")!r} to exactly one existing GM. No data written. Update the mapping explicitly if a team was renamed.')
        old = next(iter(distinct.values()))
        record = (team.get('record') or {}).get('overall') or {}
        try:
            wins = int(record['wins']); losses=int(record['losses']); ties=int(record.get('ties',0))
            pf = float(record['pointsFor']); pa=float(record['pointsAgainst'])
        except (KeyError,TypeError,ValueError) as exc:
            raise ValueError(f'Missing or invalid ESPN standings record for {old["team"]}: {exc}') from exc
        if min(wins,losses,ties,pf,pa)<0 or wins+losses+ties>14:
            raise ValueError(f'Implausible record for {old["team"]}')
        new_rows.append(dict(gm_id=old['gm_id'],team=old['team'],wins=wins,losses=losses,ties=ties,pf=round(pf,2),pa=round(pa,2)))
        match_log.append((team.get('id'),old['team']))
    if len({r['gm_id'] for r in new_rows}) != 12:
        raise ValueError('Duplicate GM mapping; refusing update')
    weeks={r['wins']+r['losses']+r['ties'] for r in new_rows}
    if len(weeks)!=1:
        raise ValueError(f'Not every team has a complete weekly record: game counts={sorted(weeks)}. ESPN results may not be finalized.')
    week=weeks.pop()
    if week<1 or week>14:
        raise ValueError(f'Unexpected regular-season completed weeks: {week}')
    if sum(r['wins'] for r in new_rows)!=sum(r['losses'] for r in new_rows):
        raise ValueError('League wins and losses do not balance; ESPN scores may still be updating')
    if sum(r['ties'] for r in new_rows)%2:
        raise ValueError('League ties do not balance')
    if abs(sum(r['pf'] for r in new_rows)-sum(r['pa'] for r in new_rows))>1.0:
        raise ValueError('League points for/against do not reconcile; aborting')
    prev = int(current.get('as_of_week',0))
    if week <= prev:
        print(f'Nothing to publish: ESPN completed week={week}, current snapshot={prev}.')
        return None
    if week > prev + 1:
        raise ValueError(f'Week jumped from {prev} to {week}; requires commissioner review before publishing')
    print(f'Validated 12 team identities and completed Week {week}.')
    print('ESPN ID → current GM team mapping:', match_log)
    if probe_only:
        print('Probe mode: no file written.')
        return None
    out = dict(current)
    out.update(as_of_week=week,status='provisional',source='ESPN Fantasy API (automated, finalized records)')
    out['standings'] = sorted(new_rows, key=lambda x:(-x['wins'], -x['ties'],-x['pf'],x['team']))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe',action='store_true',help='Validate ESPN access and mapping without saving')
    args=parser.parse_args()
    current=json.loads(DATA.read_text(encoding='utf-8'))
    proposed=validate_and_update(fetch(),current,probe_only=args.probe)
    if proposed is not None:
        DATA.write_text(json.dumps(proposed,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        print(f'Updated {DATA} to 2026 Week {proposed["as_of_week"]}.')

if __name__=='__main__':
    try: main()
    except Exception as exc:
        print('ESPN update aborted safely:',str(exc),file=sys.stderr)
        sys.exit(1)
