ALTER TABLE jobs ADD COLUMN review_transferred INTEGER NOT NULL DEFAULT 0;
CREATE TRIGGER review_returns AFTER INSERT ON reviews BEGIN
  UPDATE jobs SET review_transferred = (CASE WHEN EXISTS (
    SELECT 1 FROM reviews r WHERE r.job_id=jobs.id AND r.visitor_id=jobs.visitor_id
      AND r.revision=(SELECT MAX(r2.revision) FROM reviews r2 WHERE r2.job_id=r.job_id
        AND json_extract(r2.payload,'$.issue_id') IS json_extract(r.payload,'$.issue_id'))
      AND (json_extract(r.payload,'$.action')='return' OR json_extract(r.payload,'$.final.status')='needs_information')
  ) THEN 1 ELSE 0 END) WHERE id=NEW.job_id AND visitor_id=NEW.visitor_id;
END;
