# TradeX Autonomous — Extraction Manifest

- Source: https://github.com/MishraLtd/TradeX
- Source commit: 9dcfa3de9e2fc59db1c86dfe7796e4339dcd46b9
- Destination: https://github.com/MishraLtd/TradeX-Autonomous
- Extraction created: 2026-10-12
- Source tracked files examined: 285
- Source files copied before authored files: 279
- macOS metadata excluded: 6 .DS_Store files
- Source repository modified: no

## Mapping

| Source group | Destination | Treatment |
|---|---|---|
| PHASE2 ALLV/PHASE2/RETURNMODEL(RM) | Phase2_Monitoring/return_model | Copied all tracked files preserving internal relative paths |
| PHASE2 ALLV/PHASE2/RISKMODEL(RKM) | Phase2_Monitoring/risk_model | Copied all tracked files preserving internal relative paths |
| PHASE2 ALLV/PHASE2/MARKETREGIME(MR) | Phase2_Monitoring/market_regime | Flattened inner package one level; kept docs, DB schema and tests alongside it |
| PHASE2 ALLV/PHASE2/OPPOURTUNITYMODEL(OM) | Phase2_Monitoring/opportunity_model | Flattened inner package; retained wrapper README files under docs |
| PHASE2 ALLV/PHASE2/COSTMODEL(CM) | Phase2B_CostPnL | Canonical Cost Model, examples, tests and specifications |
| PHASE3ALLV/PHASE3V1 | Phase3_Portfolio | Full Phase 3 subtree copied intact, including local cost_model copy and vendor tests |
| No dedicated execution package found | Phase2A_Execution/README.md | Documentation only; execution placeholder remains |

All copied source file bytes are intended to remain unchanged. The machine-readable extraction_manifest.json records each source/destination path and source file SHA-256. Reorganisation changes paths only. Phase 1 is not claimed as implemented.
