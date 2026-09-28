-- The list and name limits join the quota and the value cap as per-owner settings (ADR-0019): a
-- default in the '*' row, and an override per owner where an admin set one. NULL means the default.
ALTER TABLE variable_limits
    ADD COLUMN list_items      integer CHECK (list_items >= 0),
    ADD COLUMN names_per_space integer CHECK (names_per_space >= 0);
UPDATE variable_limits SET list_items = 100, names_per_space = 200 WHERE owner_kind = '*';
