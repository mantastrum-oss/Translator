-- Translator: paid character packs (Stripe) + atomic character deduction.
-- Pricing (2026-11-15): free 15,000/month (UTC), packs valid 60 days from purchase,
-- free chars used first, then the pack that expires soonest. Bot uses the service-role key.

-- 1) Packs: one row per purchase
create table if not exists public.user_packs (
  id                 bigint generated always as identity primary key,
  user_id            text        not null,
  pack_code          text        not null,                 -- 'small' (30k) / 'large' (60k)
  chars_total        integer     not null check (chars_total > 0),
  chars_left         integer     not null check (chars_left >= 0 and chars_left <= chars_total),
  amount_cents       integer     not null check (amount_cents >= 0),
  currency           text        not null default 'eur',
  purchased_at       timestamptz not null default now(),
  expires_at         timestamptz not null,
  stripe_session_id  text        not null unique,          -- same payment can never be credited twice
  refunded_at        timestamptz,
  check (expires_at > purchased_at)
);
create index if not exists user_packs_active_idx
  on public.user_packs (user_id, expires_at)
  where chars_left > 0 and refunded_at is null;
alter table public.user_packs enable row level security;  -- no policies: service role only
-- Newer Supabase projects don't grant new tables to API roles automatically (PROD returned
-- 403 without this). Grant explicitly: the bot (service_role) only, never anon/authenticated.
revoke all on public.user_packs from anon, authenticated;
grant select, insert, update on public.user_packs to service_role;

-- 2) Monthly usage: how many of this month's chars were paid from packs
--    used_chars stays = everything translated this month (stats keep working);
--    free chars used = used_chars - paid_chars.
alter table public.user_monthly_usage
  add column if not exists paid_chars integer not null default 0 check (paid_chars >= 0);

-- 3) Status: free chars left this month + pack chars left + soonest expiry
create or replace function public.get_user_char_balance(p_user_id text, p_free_limit integer)
returns table (free_left integer, packs_left integer, next_expiry timestamptz, next_expiry_chars integer)
language sql stable security invoker set search_path = public
as $$
  with m as (
    select coalesce(sum(used_chars - paid_chars), 0)::int as free_used
    from user_monthly_usage
    where user_id = p_user_id and month = to_char(now() at time zone 'utc', 'YYYY-MM')
  ), p as (
    select chars_left, expires_at from user_packs
    where user_id = p_user_id and chars_left > 0 and refunded_at is null and expires_at > now()
  )
  select greatest(p_free_limit - m.free_used, 0),
         coalesce((select sum(chars_left) from p), 0)::int,
         (select min(expires_at) from p),
         (select sum(chars_left)::int from p where expires_at = (select min(expires_at) from p))
  from m;
$$;

-- 4) Atomic deduction after a successful translation.
--    Free chars first, then packs by soonest expiry (split across packs if needed).
--    Row locks make concurrent translations safe. Never fails because of a shortfall
--    (the check happens before translating); the shortfall is returned instead.
create or replace function public.consume_user_chars(p_user_id text, p_chars integer, p_free_limit integer)
returns table (from_free integer, from_packs integer, shortfall integer)
language plpgsql security invoker set search_path = public
as $$
declare
  v_month   text := to_char(now() at time zone 'utc', 'YYYY-MM');
  v_row     user_monthly_usage%rowtype;
  v_free    integer;
  v_need    integer;
  v_take    integer;
  v_paid    integer := 0;
  pk        record;
begin
  if p_chars <= 0 then
    return query select 0, 0, 0; return;
  end if;

  insert into user_monthly_usage (user_id, month, monthly_limit, used_chars, paid_chars)
  values (p_user_id, v_month, p_free_limit, 0, 0)
  on conflict (user_id, month) do nothing;

  select * into v_row from user_monthly_usage
  where user_id = p_user_id and month = v_month
  for update;

  v_free := least(p_chars, greatest(p_free_limit - (v_row.used_chars - v_row.paid_chars), 0));
  v_need := p_chars - v_free;

  for pk in
    select id, chars_left from user_packs
    where user_id = p_user_id and chars_left > 0 and refunded_at is null and expires_at > now()
    order by expires_at, id
    for update
  loop
    exit when v_need = 0;
    v_take := least(v_need, pk.chars_left);
    update user_packs set chars_left = chars_left - v_take where id = pk.id;
    v_need := v_need - v_take;
    v_paid := v_paid + v_take;
  end loop;

  update user_monthly_usage
     set used_chars = used_chars + p_chars,
         paid_chars = paid_chars + v_paid,
         updated_at = now()
   where user_id = p_user_id and month = v_month;

  return query select v_free, v_paid, v_need;
end;
$$;

-- Functions in public are callable via PostgREST by anyone by default: lock them to the service role.
revoke all on function public.get_user_char_balance(text, integer) from public, anon, authenticated;
revoke all on function public.consume_user_chars(text, integer, integer) from public, anon, authenticated;
grant execute on function public.get_user_char_balance(text, integer) to service_role;
grant execute on function public.consume_user_chars(text, integer, integer) to service_role;
