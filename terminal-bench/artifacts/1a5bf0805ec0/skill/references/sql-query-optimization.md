# SQL Query Optimization Reference

## Table of Contents
1. [Overview](#overview)
2. [Common Inefficiency Patterns](#common-inefficiency-patterns)
3. [Correlated Subquery → CTE + Window Function](#correlated-subquery--cte--window-function)
4. [Verification Checklist](#verification-checklist)
5. [SQLite-Specific Tips](#sqlite-specific-tips)

## Overview

When optimizing a SQL query, the goal is to produce the same output faster while preserving every structural element of the original query (SELECT columns, WHERE filters, HAVING conditions, ORDER BY, LIMIT, etc.). A query that returns the correct data but in a different order or row count is WRONG.

## Common Inefficiency Patterns

1. **Correlated subqueries in SELECT**: Each subquery runs once per outer row. Replace with CTE + window function (ROW_NUMBER, RANK, etc.).
2. **Repeated subqueries**: Same logic computed multiple times. Materialize once in a CTE.
3. **SELECT N+1**: A subquery per row fetching related data. Replace with JOIN or CTE.
4. **Unnecessary DISTINCT**: If the query already guarantees uniqueness, DISTINCT adds a sort.
5. **Missing indexes on join columns**: Check `sqlite_master` for indexes; create them if absent and the task allows.

## Correlated Subquery → CTE + Window Function

Before (slow — subquery per row):
```sql
SELECT
  w.id,
  (SELECT s.id FROM senses s WHERE s.wordid = w.id
   ORDER BY s.cnt DESC, s.id ASC LIMIT 1) AS top_id,
  (SELECT COUNT(*) FROM senses s WHERE s.wordid = w.id AND s.id =
   (SELECT s2.id FROM senses s2 WHERE s2.wordid = w.id
    ORDER BY s2.cnt DESC, s2.id ASC LIMIT 1)) AS top_cnt
FROM words w;
```

After (fast — single pass with window function):
```sql
WITH top_synset AS (
  SELECT
    s.wordid,
    s.id AS synsetid,
    COUNT(*) AS sense_count,
    ROW_NUMBER() OVER (
      PARTITION BY s.wordid
      ORDER BY COUNT(*) DESC, s.id ASC
    ) AS rn
  FROM senses s
  GROUP BY s.wordid, s.id
)
SELECT w.id, ts.synsetid AS top_id, ts.sense_count AS top_cnt
FROM words w
LEFT JOIN top_synset ts ON ts.wordid = w.id AND ts.rn = 1;
```

Key: ROW_NUMBER with PARTITION BY replaces the correlated subquery that finds the "top" row per group. The `rn = 1` filter in the JOIN picks the top row.

## Verification Checklist

Before writing the optimized query, re-read the original query and extract EVERY structural element:
- [ ] All SELECT columns and their aliases (column names must match exactly including case)
- [ ] All WHERE conditions
- [ ] All HAVING conditions
- [ ] ORDER BY clause (column names, ASC/DESC)
- [ ] **LIMIT clause** — agents frequently omit this when rewriting, producing too many rows
- [ ] JOIN types (INNER vs LEFT vs CROSS)

After writing the optimized query, verify:
1. **Row count matches**: `wc -l` on both outputs must be equal
2. **Content matches**: `diff <(original) <(optimized)` produces no output
3. **Column names match**: The first line (header) must be identical
4. **No database modifications**: The database file hash must be preserved

## SQLite-Specific Tips

- SQLite does not support `FILTER` clause in window functions (use CASE expressions instead)
- Use `EXPLAIN QUERY PLAN` to inspect the query plan before/after optimization
- Run queries with `time sqlite3 db < query.sql > output.txt` to measure performance
- Always redirect output to a file — running large queries in the foreground terminal blocks it
- SQLite auto-creates indexes for PRIMARY KEY and UNIQUE constraints; check for others with:
  `SELECT name FROM sqlite_master WHERE type='index'`
- For query comparison, use `sqlite3` in pipe mode with `.separator |` for clean output