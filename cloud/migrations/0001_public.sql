CREATE TABLE visitors (
  id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, csrf TEXT NOT NULL,
  ip_hash TEXT NOT NULL, day TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE INDEX visitors_expiry ON visitors(expires_at);
CREATE INDEX visitors_ip_day ON visitors(ip_hash,day);
CREATE INDEX visitors_day ON visitors(day);
CREATE TRIGGER visitor_limits BEFORE INSERT ON visitors BEGIN
  SELECT (CASE WHEN (SELECT COUNT(*) FROM visitors WHERE day=NEW.day)>=300 OR
    (SELECT COUNT(*) FROM visitors WHERE ip_hash=NEW.ip_hash AND day=NEW.day)>=30 OR
    (SELECT COUNT(*) FROM visitors)>=2000
  THEN RAISE(ABORT,'visitor_quota') END);
END;
CREATE TABLE cases (
  visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
  id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(visitor_id,id)
);
CREATE TRIGGER case_limits BEFORE INSERT ON cases BEGIN
  SELECT (CASE WHEN (SELECT COUNT(*) FROM cases WHERE visitor_id=NEW.visitor_id)>=30
    OR (SELECT COUNT(*) FROM cases)>=2000 THEN RAISE(ABORT,'case_quota') END);
END;
CREATE TABLE jobs (
  id TEXT PRIMARY KEY, visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
  request_key TEXT NOT NULL, case_id TEXT NOT NULL, input TEXT NOT NULL,
  ip_hash TEXT NOT NULL, day TEXT NOT NULL, created_at TEXT NOT NULL, bundle_id TEXT NOT NULL,
  state TEXT NOT NULL, stage TEXT NOT NULL, error TEXT,
  result TEXT, result_sha256 TEXT, risk TEXT NOT NULL DEFAULT '待定',
  priority TEXT NOT NULL DEFAULT 'priority', revision INTEGER NOT NULL DEFAULT 0,
  UNIQUE(visitor_id,request_key)
);
CREATE INDEX jobs_visitor_time ON jobs(visitor_id,created_at);
CREATE INDEX jobs_visitor_day ON jobs(visitor_id,day);
CREATE INDEX jobs_ip_day ON jobs(ip_hash,day);
CREATE INDEX jobs_day ON jobs(day);
CREATE INDEX jobs_state_time ON jobs(state,created_at);
CREATE TRIGGER job_limits BEFORE INSERT ON jobs BEGIN
  SELECT (CASE WHEN (SELECT COUNT(*) FROM jobs WHERE day=NEW.day)>=40 OR
    (SELECT COUNT(*) FROM jobs WHERE visitor_id=NEW.visitor_id AND day=NEW.day)>=6 OR
    (SELECT COUNT(*) FROM jobs WHERE ip_hash=NEW.ip_hash AND day=NEW.day)>=12 OR
    (SELECT COUNT(*) FROM jobs)>=1500 THEN RAISE(ABORT,'analysis_quota') END);
  SELECT (CASE WHEN (SELECT COUNT(*) FROM jobs WHERE state='running' AND created_at>strftime('%Y-%m-%dT%H:%M:%fZ','now','-2 minutes'))>=3 OR
    EXISTS(SELECT 1 FROM jobs WHERE visitor_id=NEW.visitor_id AND state='running' AND created_at>strftime('%Y-%m-%dT%H:%M:%fZ','now','-2 minutes'))
    THEN RAISE(ABORT,'analysis_busy') END);
END;
CREATE TABLE reviews (
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL, request_key TEXT NOT NULL, request_hash TEXT NOT NULL,
  actor TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY(job_id,revision), UNIQUE(visitor_id,request_key)
);
CREATE TRIGGER review_guard BEFORE INSERT ON reviews BEGIN
  SELECT (CASE WHEN NOT EXISTS(SELECT 1 FROM jobs WHERE id=NEW.job_id AND visitor_id=NEW.visitor_id AND revision=NEW.revision-1 AND result IS NOT NULL)
    THEN RAISE(ABORT,'review_conflict') END);
  SELECT (CASE WHEN NEW.revision>50 THEN RAISE(ABORT,'review_quota') END);
END;
CREATE TRIGGER review_revision AFTER INSERT ON reviews BEGIN
  UPDATE jobs SET revision=NEW.revision WHERE id=NEW.job_id AND visitor_id=NEW.visitor_id;
END;
CREATE TABLE alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
  state TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX alerts_job ON alerts(job_id,id);
CREATE TRIGGER alert_guard BEFORE INSERT ON alerts BEGIN
  SELECT (CASE WHEN NOT EXISTS(SELECT 1 FROM jobs WHERE id=NEW.job_id AND visitor_id=NEW.visitor_id AND result IS NOT NULL)
    THEN RAISE(ABORT,'alert_conflict') END);
  SELECT (CASE WHEN (SELECT COUNT(*) FROM alerts WHERE job_id=NEW.job_id)>=30 THEN RAISE(ABORT,'alert_quota') END);
END;
