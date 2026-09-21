-- Durable record of every Stripe webhook event (montyesim-eshop-api#6).
-- Run in Supabase SQL editor BEFORE merging the PR; the code needs the table.
create table if not exists public.stripe_event (
    id            text primary key,              -- Stripe event id: blocks duplicate deliveries
    type          text,
    status        text not null default 'pending', -- pending | processing | processed | failed
    attempts      integer not null default 0,
    last_error    text,
    created_at    timestamptz not null default now(),
    processed_at  timestamptz
);

create index if not exists stripe_event_status_created_idx
    on public.stripe_event (status, created_at);

-- The API connects with the service role, which bypasses RLS. Enable RLS with no policies
-- so that the anon/authenticated keys cannot read payment events.
alter table public.stripe_event enable row level security;

-- Check afterwards:
-- select status, count(*) from public.stripe_event group by status;
