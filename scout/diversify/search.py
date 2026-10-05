#!/usr/bin/env python3
"""Scout strategy discovery for non-trend, mean-reversion and cross-sectional strategies.
Usage: python3 search.py --token <GITHUB_TOKEN> --top-n 10
Searches GitHub, arXiv, and SSRN for mean-reversion, cross-sectional reversal, funding-rate cross-section,
pairs/stat-arb, and basis strategies in crypto with published backtests."""

import json
import sys
import time
import urllib.request
import urllib.parse
import argparse
from typing import List, Dict, Any


def github_search(token: str, query: str, min_stars: int = 10) -> List[Dict[str, Any]]:
    """Search GitHub repositories using the Search API."""
    results = []
    headers = {
        'Accept': 'application/vnd.github.v3+json',
        'Authorization': f'token {token}',
        'User-Agent': 'GiiQ-Scout'
    }
    
    encoded_query = urllib.parse.quote(query)
    url = f'https://api.github.com/search/repositories?q={encoded_query}+language:python+stars:>={min_stars}&sort=stars&order=desc&per_page=30'
    
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            
            for item in data.get('items', [])[:15]:
                # Fetch README to check for backtest mentions
                readme_url = f"https://api.github.com/repos/{item['full_name']}/readme"
                has_backtest = False
                pf_mentioned = False
                
                try:
                    readme_req = urllib.request.Request(readme_url, headers=headers)
                    with urllib.request.urlopen(readme_req, timeout=10) as readme_resp:
                        readme_data = json.loads(readme_resp.read().decode())
                        content = readme_data.get('content', '')
                        # GitHub returns base64 encoded content
                        import base64
                        decoded = base64.b64decode(content).decode('utf-8', errors='ignore').lower()
                        
                        backtest_keywords = ['backtest', 'backtesting', 'historical test', 'performance']
                        has_backtest = any(kw in decoded for kw in backtest_keywords)
                        
                        metric_keywords = ['sharpe', 'profit factor', 'pf', 'max drawdown', 'mdd', 'cagr', 'return']
                        pf_mentioned = has_backtest and any(kw in decoded for kw in metric_keywords)
                        
                        time.sleep(0.5)  # Rate limit
                except Exception:
                    pass
                
                results.append({
                    'repo': item['full_name'],
                    'url': item['html_url'],
                    'stars': item['stargazers_count'],
                    'description': item.get('description', ''),
                    'last_push': item['pushed_at'][:10],
                    'has_backtest_mention': has_backtest,
                    'has_metrics': pf_mentioned
                })
                
        time.sleep(1)  # Rate limit between searches
        
    except Exception as e:
        print(f"GitHub search error for '{query}': {e}", file=sys.stderr)
    
    return results


def arxiv_search(query: str, max_results: int = 10) -> List[Dict[str, Any]]:
    """Search arXiv for academic papers."""
    results = []
    base_url = 'http://export.arxiv.org/api/query'
    
    search_query = f'all:{query} AND (cat:q-fin.TR OR cat:q-fin.CP OR cat:cs.CE)'
    params = {
        'search_query': search_query,
        'start': 0,
        'max_results': max_results,
        'sortBy': 'relevance',
        'sortOrder': 'descending'
    }
    
    url = f"{base_url}?{urllib.parse.urlencode(params)}"
    
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            content = response.read().decode('utf-8')
            
            # Parse XML manually (avoid external dependencies)
            entries = content.split('<entry>')[1:]
            
            for entry in entries[:max_results]:
                try:
                    title = entry.split('<title>')[1].split('</title>')[0].strip()
                    arxiv_id = entry.split('<id>')[1].split('</id>')[0].strip()
                    published = entry.split('<published>')[1].split('</published>')[0][:10]
                    summary = entry.split('<summary>')[1].split('</summary>')[0].strip()[:200]
                    
                    results.append({
                        'title': title,
                        'id': arxiv_id.split('/')[-1],
                        'url': arxiv_id,
                        'published': published,
                        'summary': summary
                    })
                except Exception:
                    continue
        
        time.sleep(3)  # ArXiv rate limit
        
    except Exception as e:
        print(f"arXiv search error for '{query}': {e}", file=sys.stderr)
    
    return results


def ssrn_search_via_google(query: str) -> List[Dict[str, Any]]:
    """Search SSRN via Google (since SSRN has no public API)."""
    results = []
    
    # Use Google search for SSRN papers
    google_query = f"site:ssrn.com {query} cryptocurrency"
    encoded = urllib.parse.quote(google_query)
    url = f"https://www.google.com/search?q={encoded}&num=10"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as response:
            content = response.read().decode('utf-8', errors='ignore')
            
            # Simple extraction (not perfect but workable)
            ssrn_links = []
            for part in content.split('https://papers.ssrn.com'):
                if 'abstract=' in part or 'sol3/papers.cfm' in part:
                    link = 'https://papers.ssrn.com' + part.split('"')[0].split('&')[0]
                    if link not in ssrn_links and len(ssrn_links) < 5:
                        ssrn_links.append(link)
            
            for link in ssrn_links:
                results.append({
                    'title': f'SSRN Paper (ID: {link.split("=")[-1][:20]})',
                    'url': link,
                    'note': 'Manual review required - Google search result'
                })
        
        time.sleep(5)  # Be nice to Google
        
    except Exception as e:
        print(f"SSRN search error for '{query}': {e}", file=sys.stderr)
    
    return results


def search_strategies(token: str, top_n: int = 10) -> Dict[str, Any]:
    """Search for non-trend crypto strategies across GitHub, arXiv, and SSRN."""
    
    print("Searching for non-trend crypto strategies...")
    print("=" * 80)
    
    all_results = {
        'github': [],
        'arxiv': [],
        'ssrn': []
    }
    
    # GitHub search queries targeting non-trend strategies
    github_queries = [
        'cryptocurrency mean reversion trading',
        'crypto cross-sectional reversal',
        'crypto funding rate arbitrage',
        'cryptocurrency pairs trading statistical arbitrage',
        'crypto basis trading futures spot',
        'cryptocurrency market neutral strategy',
        'crypto short-term reversal',
    ]
    
    print("\n[1/3] Searching GitHub...")
    for query in github_queries:
        print(f"  - Query: {query}")
        results = github_search(token, query, min_stars=10)
        all_results['github'].extend(results)
    
    # Deduplicate GitHub results by repo name
    seen_repos = set()
    unique_github = []
    for r in all_results['github']:
        if r['repo'] not in seen_repos:
            seen_repos.add(r['repo'])
            unique_github.append(r)
    all_results['github'] = sorted(unique_github, key=lambda x: x['stars'], reverse=True)[:top_n]
    
    print(f"  Found {len(all_results['github'])} unique repositories")
    
    # arXiv search
    arxiv_queries = [
        'cryptocurrency mean reversion',
        'cryptocurrency cross-sectional',
        'cryptocurrency funding rate',
        'cryptocurrency statistical arbitrage'
    ]
    
    print("\n[2/3] Searching arXiv...")
    for query in arxiv_queries:
        print(f"  - Query: {query}")
        results = arxiv_search(query, max_results=3)
        all_results['arxiv'].extend(results)
    
    # Deduplicate arXiv by ID
    seen_arxiv = set()
    unique_arxiv = []
    for r in all_results['arxiv']:
        if r['id'] not in seen_arxiv:
            seen_arxiv.add(r['id'])
            unique_arxiv.append(r)
    all_results['arxiv'] = unique_arxiv[:5]
    
    print(f"  Found {len(all_results['arxiv'])} papers")
    
    # SSRN search (best-effort via Google)
    print("\n[3/3] Searching SSRN (via Google)...")
    ssrn_queries = [
        'cryptocurrency reversal',
        'cryptocurrency cross-sectional momentum'
    ]
    
    for query in ssrn_queries[:1]:  # Limit to avoid rate limits
        print(f"  - Query: {query}")
        results = ssrn_search_via_google(query)
        all_results['ssrn'].extend(results)
    
    all_results['ssrn'] = all_results['ssrn'][:3]
    print(f"  Found {len(all_results['ssrn'])} papers")
    
    return all_results


def main():
    parser = argparse.ArgumentParser(description='Search for non-trend crypto strategies')
    parser.add_argument('--token', required=True, help='GitHub API token')
    parser.add_argument('--top-n', type=int, default=10, help='Number of top results to return')
    parser.add_argument('--output', default='scout/diversify/out/search_results.json', help='Output JSON file')
    
    args = parser.parse_args()
    
    results = search_strategies(args.token, args.top_n)
    
    # Save results
    with open(args.output, 'w') as f:
        json.dump(results, f, indent=2)
    
    print("\n" + "=" * 80)
    print(f"Search complete. Results saved to {args.output}")
    print(f"  GitHub repos: {len(results['github'])}")
    print(f"  arXiv papers: {len(results['arxiv'])}")
    print(f"  SSRN papers: {len(results['ssrn'])}")
    
    # Print summary
    print("\n" + "=" * 80)
    print("TOP GITHUB REPOSITORIES:")
    print("=" * 80)
    for i, repo in enumerate(results['github'][:10], 1):
        backtest_status = '✓ backtest+metrics' if repo['has_metrics'] else ('✓ backtest' if repo['has_backtest_mention'] else '✗ no backtest')
        print(f"{i}. {repo['repo']} ({repo['stars']} ⭐)")
        print(f"   {repo['url']}")
        print(f"   Last push: {repo['last_push']} | {backtest_status}")
        print(f"   {repo['description'][:100]}")
        print()


if __name__ == '__main__':
    main()
