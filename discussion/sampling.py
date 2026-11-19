"""Reproducible platform/date strata, rotating posts, with content-keyed labels."""
import hashlib
import re
import unicodedata
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = 'platform-date-post-v1'
PROMPT_VERSION = 'sentiment-v2'


def normalized(text):
    return ' '.join(unicodedata.normalize('NFKC', text).split())


def classify(store, provider, comments, limit=500, seed='discussion', stopped=lambda: False):
    groups, duplicates = {}, Counter()
    for c in comments:
        text = normalized(c['text'])
        if not text:
            continue
        duplicates[c.get('source', '未注明')] += 1
        # Stable representative, independent of acquisition order.
        if text not in groups or c['id'] < groups[text]['id']:
            groups[text] = c
    unique = list(groups.values())
    strata = defaultdict(lambda: defaultdict(list))
    rank = lambda value: hashlib.sha256((seed + value).encode()).hexdigest()
    for c in unique:
        date = re.search(r'\d{4}-\d{2}-\d{2}', c.get('time', ''))
        key = (c.get('platform') or c.get('source', '未注明')) + ' / ' + (date[0] if date else '日期未知')
        strata[key][c.get('parent_id') or c['id']].append(c)
    queues = {}
    for key, posts in strata.items():
        post_queues = deque(deque(sorted(posts[p], key=lambda c: rank(c['id']))) for p in sorted(posts, key=rank))
        ordered = []
        while post_queues:
            q = post_queues.popleft()
            ordered.append(q.popleft())
            if q:
                post_queues.append(q)
        queues[key] = deque(ordered)
    active = deque(sorted(queues, key=rank))
    selected, allocations = [], Counter()
    while active and len(selected) < limit:
        key = active.popleft()
        selected.append(queues[key].popleft())
        allocations[key] += 1
        if queues[key]:
            active.append(key)
    identity = provider.sentiment_identity() if hasattr(provider, 'sentiment_identity') else type(provider).__module__ + '.' + type(provider).__name__
    def cache_key(c):
        return hashlib.sha256((identity + PROMPT_VERSION + normalized(c['text'])).encode()).hexdigest()
    counts = {'负面': 0, '中性': 0, '正面': 0}
    labels, missing = [], []
    for c in selected:
        label = store.cached_label(cache_key(c))
        if label in counts:
            labels.append(dict(id=c['id'], label=label))
        else:
            missing.append(c)
    def classify_batch(batch):
        for attempt in range(2):
            if stopped():
                return {}
            try:
                return provider.json('sentiment', '判断评论表达的情感（不是对特定人物的立场）。返回 {"labels":[{"id":"原ID","label":"负面/中性/正面"}]}。每条恰好一项。', {'comments': batch, '_max_tokens': 2200})
            except Exception as exc:
                if attempt or not any(x in str(exc) for x in ('Timeout','Connection','HTTP 500','HTTP 502','HTTP 503','HTTP 504')):
                    return {}
    batches = [missing[start:start+25] for start in range(0, len(missing), 25)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(classify_batch, batch): batch for batch in batches}
        for future in as_completed(futures):
            batch, result = futures[future], future.result()
            try:
                entries = result.get('labels', [])
                seen = Counter(x.get('id') for x in entries if isinstance(x, dict) and isinstance(x.get('id'), str))
                by_id = {x['id']: x.get('label') for x in entries if isinstance(x, dict) and isinstance(x.get('id'), str)}
                for c in batch:
                    label = by_id.get(c['id'])
                    if seen[c['id']]==1 and label in counts:
                        store.cached_label(cache_key(c), label)
                        labels.append(dict(id=c['id'], label=label))
            except Exception:
                continue
    order = {c['id']: i for i, c in enumerate(selected)}
    labels.sort(key=lambda x: order[x['id']])
    for entry in labels:
        counts[entry['label']] += 1
    done = len(labels)
    return dict(method='配置的大模型分批分类，未做人工准确率评估', total=len(comments), unique=len(unique),
                sampled=len(selected), sample_limit=limit, classified=done, failed=len(selected)-done,
                counts=counts, labels=labels, percentages={k: round(v*100/done, 1) if done else None for k,v in counts.items()},
                scope='百分比以成功分类的抽样评论为分母，仅描述本次分析样本；不推断全部已采评论或公众。平台、日期均衡分配，层内帖子轮转，非总体概率抽样。',
                sources=dict(Counter(c.get('source', '未注明') for c in unique)), raw_sources=dict(duplicates),
                sampling=dict(version=VERSION, seed=seed, ids=[c['id'] for c in selected], allocations=dict(allocations),
                              uncovered_strata=sorted(set(strata)-set(allocations)), prompt_version=PROMPT_VERSION))
