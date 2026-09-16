# Short-turn stop mapping test

Temporary branch-only note. This branch reconstructs physical vehicle sequence as `stop_seq - remaining_stops`, maps an exact sequence to route stop metadata when available, and otherwise labels the position explicitly as `物理seqN（站名待映射）` rather than reusing the downstream queried backbone stop name.

Production classification thresholds are unchanged. Merge only after Pudong35 17590/62981 positives and the seven Pudong78 historical negatives pass regression.