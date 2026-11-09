-- Ko-fi one-time donations and atomic quota grants.
-- Run this after schema_user_quota.sql in the same Supabase project.

create table if not exists public.donations (
    id uuid primary key default gen_random_uuid(),
    kofi_transaction_id text not null unique,
    user_id text not null,
    amount numeric(10, 2) not null,
    currency text not null,
    chars_added integer not null,
    raw_payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create index if not exists donations_user_id_idx
on public.donations (user_id);

alter table public.donations enable row level security;

create policy "Service role access only"
    on public.donations
    for all
    using (auth.role() = 'service_role')
    with check (auth.role() = 'service_role');

create or replace function public.apply_donation(
    p_transaction_id text,
    p_user_id text,
    p_amount numeric,
    p_currency text,
    p_chars_added integer,
    p_payload jsonb
)
returns table(applied boolean, status text)
language plpgsql
security definer
set search_path = public
as $$
declare
    current_month text := to_char(timezone('utc', now()), 'YYYY-MM');
begin
    if p_chars_added <= 0 then
        return query select false, 'invalid_bonus';
        return;
    end if;

    insert into public.donations (
        kofi_transaction_id,
        user_id,
        amount,
        currency,
        chars_added,
        raw_payload
    ) values (
        p_transaction_id,
        p_user_id,
        p_amount,
        p_currency,
        p_chars_added,
        p_payload
    ) on conflict (kofi_transaction_id) do nothing;

    if not found then
        return query select false, 'duplicate';
        return;
    end if;

    insert into public.user_monthly_usage (
        user_id,
        month,
        monthly_limit,
        used_chars,
        updated_at
    ) values (
        p_user_id,
        current_month,
        50000 + p_chars_added,
        0,
        now()
    ) on conflict (user_id, month) do update set
        monthly_limit = public.user_monthly_usage.monthly_limit + p_chars_added,
        updated_at = now();

    return query select true, 'applied';
end;
$$;

revoke all on function public.apply_donation(text, text, numeric, text, integer, jsonb) from public;
grant execute on function public.apply_donation(text, text, numeric, text, integer, jsonb) to service_role;
