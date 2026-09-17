-- Keep a login snapshot next to role memberships so admin listings don't need a Twitch lookup.
ALTER TABLE role_members ADD COLUMN user_login TEXT;
ALTER TABLE global_admins ADD COLUMN user_login TEXT;
ALTER TABLE ignore_list ADD COLUMN user_login TEXT;
