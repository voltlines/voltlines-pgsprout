-- Gate before db1_golden is locked: one (label, count) row per check, every count must be 0.
SELECT 'real-looking email' AS problem, count(*) FROM users
 WHERE email IS NOT NULL AND email NOT LIKE '%@example.invalid'
UNION ALL
SELECT 'real-looking phone', count(*) FROM users
 WHERE phone_number IS NOT NULL AND phone_number !~ '^\+900[0-9]{9}$'
UNION ALL
SELECT 'live tokens', count(*) FROM api_tokens;
