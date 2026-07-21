| Case | Flow | Google | LLM Tokens | Browser |
|---|---|---:|---:|---:|
| Best case | Tier 0 cached URL / known metadata | 0 queries | 5k input + 500 output | No |
| Average case | Google + official site + ranking | 3 queries | 20k input + 2k output | No |
| Worst case | Multi-domain + browser fallback | 10 queries | 100k input + 10k output | Yes, short ECS run |


| Case | Claude Sonnet 5 | Amazon Nova 2 Lite |
|---|---:|---:|
| Best case | `$15.00` | `$2.75` |
| Average case | `$75.00` | `$26.00` |
| Worst case | `$362.00` | `$117.00` |