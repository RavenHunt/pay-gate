WITH picked AS (
    SELECT id
    FROM gateway_jobs
    WHERE job_type = ANY(CAST(:job_types AS text[]))
      AND (
            (status IN ('PENDING', 'RETRY') AND run_after <= now())
         OR (status = 'RUNNING' AND locked_until < now())
          )
    ORDER BY run_after, id
    LIMIT :batch_size
    FOR UPDATE SKIP LOCKED
)
UPDATE gateway_jobs j
SET status       = 'RUNNING',
    attempts     = j.attempts + 1,
    locked_by    = :run_id,
    locked_until = now() + make_interval(secs => :lease_seconds),
    updated_at   = now()
FROM picked
WHERE j.id = picked.id
RETURNING j.*
