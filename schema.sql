-- Team telemetry schema for the System Monitor Widget.
--
-- Run this once in the Supabase SQL editor (Dashboard -> SQL Editor -> New
-- query -> paste -> Run). It is idempotent, so re-running it after an upgrade
-- is safe.
--
-- The security model is the whole point of this file, so read it before you
-- change it:
--
--   Every laptop carries the project's anon key, because there is no way to
--   give a desktop agent a secret it can keep. That key is therefore granted
--   INSERT (and, for the two "current state" tables, UPDATE) and nothing else.
--   A key lifted off a teammate's machine can add rows; it cannot read one
--   back. This matters because `tasks.prompt` holds what people typed.
--
--   Reading is granted to the `authenticated` role only, and only for the team
--   the signed-in user belongs to. So a dashboard user signs in with a real
--   Supabase Auth account, and seeing the board is a privilege you grant per
--   person by inserting a row in `team_members`.
--
-- Tables divide by how they age:
--   tasks          append-only history. Immutable; no UPDATE policy at all.
--   live_sessions  presence. Upserted; judged stale by age, never deleted by
--                  a reporter, because DELETE on an anon key would let anyone
--                  with a laptop wipe the board.
--   health         one row per laptop, overwritten.
--   devices        one row per laptop, doubles as the heartbeat.

-- ---------------------------------------------------------------- devices --

create table if not exists public.devices (
  device_id     text primary key,
  team_id       text not null,
  member        text not null default '',
  host          text not null default '',
  platform      text not null default '',
  agent_version text not null default '',
  first_seen    timestamptz not null default now(),
  last_seen     timestamptz not null default now()
);

create index if not exists devices_team_idx
  on public.devices (team_id, last_seen desc);

-- ------------------------------------------------------------------ tasks --

create table if not exists public.tasks (
  team_id          text not null,
  device_id        text not null,
  task_key         text not null,
  member           text not null default '',
  session_id       text not null default '',
  project          text not null default 'agent',
  -- Empty when the member has share_prompts off. The column still exists so
  -- the dashboard does not have to special-case their rows.
  prompt           text not null default '',
  started_at       timestamptz,
  ended_at         timestamptz not null,
  duration_s       double precision,
  blocked_s        double precision not null default 0,
  waits            integer not null default 0,
  max_reply_s      double precision,
  tok_in           bigint not null default 0,
  tok_out          bigint not null default 0,
  tok_cache_create bigint not null default 0,
  tok_cache_read   bigint not null default 0,
  tokens           bigint not null default 0,
  model            text not null default '',
  tools            integer not null default 0,
  received_at      timestamptz not null default now(),
  -- (device, task_key) is what makes a re-push idempotent: the reporter's
  -- watermark overlaps on purpose, and the duplicates land here and are
  -- ignored rather than double-counting a turn.
  primary key (device_id, task_key)
);

create index if not exists tasks_team_time_idx
  on public.tasks (team_id, ended_at desc);
create index if not exists tasks_team_member_idx
  on public.tasks (team_id, member, ended_at desc);

-- ---------------------------------------------------------- live_sessions --

create table if not exists public.live_sessions (
  team_id      text not null,
  device_id    text not null,
  session_key  text not null,
  member       text not null default '',
  project      text not null default '',
  state        text not null default 'working',
  message      text not null default '',
  state_since  timestamptz,
  updated_at   timestamptz not null default now(),
  primary key (device_id, session_key)
);

create index if not exists live_sessions_team_idx
  on public.live_sessions (team_id, updated_at desc);

-- ----------------------------------------------------------------- health --

create table if not exists public.health (
  device_id    text primary key,
  team_id      text not null,
  member       text not null default '',
  cpu_pct      double precision,
  ram_pct      double precision,
  ram_used_gb  double precision,
  ram_total_gb double precision,
  gpu_pct      double precision,
  gpu_temp     double precision,
  cpu_temp     double precision,
  fan_rpm      double precision,
  sampled_at   timestamptz not null default now()
);

create index if not exists health_team_idx
  on public.health (team_id, sampled_at desc);

-- ----------------------------------------------------------- membership ---
--
-- Who is allowed to READ which team's board. Add a row per dashboard user:
--
--   insert into public.team_members (user_id, team_id)
--   select id, 'acme' from auth.users where email = 'lead@acme.com';

create table if not exists public.team_members (
  user_id uuid primary key references auth.users (id) on delete cascade,
  team_id text not null,
  role    text not null default 'viewer',
  added_at timestamptz not null default now()
);

create index if not exists team_members_team_idx
  on public.team_members (team_id);

-- SECURITY DEFINER so the read policies below can consult this table without
-- needing a SELECT policy on it, which would otherwise recurse: the policy on
-- team_members would itself have to read team_members.
create or replace function public.my_team_id()
  returns text
  language sql
  stable
  security definer
  set search_path = public
as $$
  select team_id from public.team_members where user_id = auth.uid()
$$;

revoke all on function public.my_team_id() from public;
grant execute on function public.my_team_id() to authenticated;

-- ------------------------------------------------------------------- RLS --

alter table public.devices       enable row level security;
alter table public.tasks         enable row level security;
alter table public.live_sessions enable row level security;
alter table public.health        enable row level security;
alter table public.team_members  enable row level security;

-- Reporters: write only. `team_id <> ''` is a sanity guard, not a security
-- boundary -- an anon key cannot be scoped to a team, so a laptop could in
-- principle report under any team_id. The protection that matters is that it
-- can never read, so the worst case is noise in a team you can audit by
-- device_id, not disclosure.
drop policy if exists devices_insert_anon on public.devices;
create policy devices_insert_anon on public.devices
  for insert to anon, authenticated with check (team_id <> '');

drop policy if exists devices_update_anon on public.devices;
create policy devices_update_anon on public.devices
  for update to anon, authenticated using (true) with check (team_id <> '');

drop policy if exists tasks_insert_anon on public.tasks;
create policy tasks_insert_anon on public.tasks
  for insert to anon, authenticated with check (team_id <> '');

drop policy if exists sessions_insert_anon on public.live_sessions;
create policy sessions_insert_anon on public.live_sessions
  for insert to anon, authenticated with check (team_id <> '');

drop policy if exists sessions_update_anon on public.live_sessions;
create policy sessions_update_anon on public.live_sessions
  for update to anon, authenticated using (true) with check (team_id <> '');

drop policy if exists health_insert_anon on public.health;
create policy health_insert_anon on public.health
  for insert to anon, authenticated with check (team_id <> '');

drop policy if exists health_update_anon on public.health;
create policy health_update_anon on public.health
  for update to anon, authenticated using (true) with check (team_id <> '');

-- Viewers: read only, and only their own team.
drop policy if exists devices_select_team on public.devices;
create policy devices_select_team on public.devices
  for select to authenticated using (team_id = public.my_team_id());

drop policy if exists tasks_select_team on public.tasks;
create policy tasks_select_team on public.tasks
  for select to authenticated using (team_id = public.my_team_id());

drop policy if exists sessions_select_team on public.live_sessions;
create policy sessions_select_team on public.live_sessions
  for select to authenticated using (team_id = public.my_team_id());

drop policy if exists health_select_team on public.health;
create policy health_select_team on public.health
  for select to authenticated using (team_id = public.my_team_id());

-- A member may see their own membership row and nothing else. Granting the
-- whole team's rows here would leak the roster to every viewer.
drop policy if exists team_members_select_self on public.team_members;
create policy team_members_select_self on public.team_members
  for select to authenticated using (user_id = auth.uid());

-- -------------------------------------------------------------- retention --
--
-- Presence rows are never deleted by reporters (see the note at the top), so
-- something has to prune them. Call this from pg_cron, or from the SQL editor
-- whenever you remember to.

create or replace function public.prune_telemetry(
  keep_task_days int default 180,
  keep_presence_hours int default 48
)
  returns void
  language plpgsql
  security definer
  set search_path = public
as $$
begin
  delete from public.live_sessions
    where updated_at < now() - make_interval(hours => keep_presence_hours);
  delete from public.tasks
    where ended_at < now() - make_interval(days => keep_task_days);
end
$$;

-- With the pg_cron extension enabled (Database -> Extensions -> pg_cron):
--
--   select cron.schedule('prune-telemetry', '17 4 * * *',
--                        $$select public.prune_telemetry()$$);
