-- Per-User Monthly Character Quota Table
-- Run this in your Dev Supabase project to set up quota tracking

-- Create the user_monthly_usage table
create table if not exists public.user_monthly_usage (
    user_id text not null,
    month text not null,
    monthly_limit integer not null default 50000,
    used_chars integer not null default 0,
    reset_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (user_id, month)
);

-- Create index for fast lookups by user_id
create index if not exists user_monthly_usage_user_id_idx
on public.user_monthly_usage (user_id);

-- Create index for fast lookups by month
create index if not exists user_monthly_usage_month_idx
on public.user_monthly_usage (month);

-- Enable Row Level Security (optional but recommended)
-- Disable public access; only service role can access
alter table public.user_monthly_usage enable row level security;

-- Create policy for service role (bot) access
create policy "Service role access only"
    on public.user_monthly_usage
    for all
    using (auth.role() = 'service_role')
    with check (auth.role() = 'service_role');
