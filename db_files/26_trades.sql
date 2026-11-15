CREATE OR REPLACE VIEW trades AS
WITH cash_legs AS (
  SELECT t.transaction_id , t.event_date, t.account_id, tl.role, tl.amount, tl.currency, f.rate, CAST(tl.amount / CASE WHEN tl.currency = 'EUR' THEN 1 ELSE f.rate END AS DECIMAL(18, 6)) AS amount_eur, t.is_reinvestment
  FROM transaction_legs tl
  JOIN transactions t ON tl.transaction_id = t.transaction_id
  LEFT JOIN daily_fx f 
    ON tl.currency = f.quote_currency
    AND t.event_date = f.date
  WHERE tl.role IN ('cost', 'proceeds', 'fee')
  AND t.type IN ('buy', 'sell')
),
share_legs AS (
  SELECT transaction_id, instrument_id, amount AS quantity, unit_price, price_currency
  FROM transaction_legs
  WHERE role = 'shares'
),
trade_cash AS (
  SELECT c.transaction_id, c.event_date, c.account_id, s.instrument_id, s.quantity, s.unit_price, s.price_currency, c.is_reinvestment, SUM(c.amount_eur) AS cash_eur
  FROM cash_legs c
  JOIN share_legs s ON c.transaction_id = s.transaction_id
  GROUP BY c.transaction_id, c.event_date, c.account_id, s.instrument_id, s.quantity, s.unit_price, s.price_currency, c.is_reinvestment
)

SELECT * FROM trade_cash;