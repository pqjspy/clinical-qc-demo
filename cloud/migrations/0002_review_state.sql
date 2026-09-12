ALTER TABLE jobs ADD COLUMN review_closed INTEGER NOT NULL DEFAULT 0;
CREATE TRIGGER review_settlement AFTER INSERT ON reviews BEGIN
  UPDATE jobs SET review_closed = (CASE WHEN
    json_array_length(json_extract(result,'$.issues'))>0 AND NOT EXISTS (
      SELECT 1 FROM json_each(json_extract(jobs.result,'$.issues')) issue
      WHERE COALESCE((SELECT CASE WHEN json_extract(r.payload,'$.action')='confirm' OR
        (json_extract(r.payload,'$.action')='edit' AND json_extract(r.payload,'$.final.status') IN ('proposed_findings','no_finding_for_checked_rule'))
        THEN 1 ELSE 0 END FROM reviews r
        WHERE r.job_id=jobs.id AND r.visitor_id=jobs.visitor_id
          AND json_extract(r.payload,'$.issue_id')=json_extract(issue.value,'$.issue_id')
        ORDER BY r.revision DESC LIMIT 1),0)=0
    ) THEN 1 ELSE 0 END)
    WHERE id=NEW.job_id AND visitor_id=NEW.visitor_id;
END;
