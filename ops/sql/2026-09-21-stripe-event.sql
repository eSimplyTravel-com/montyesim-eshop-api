-- Durable record of every Stripe webhook event (montyesim-eshop-api#6).
-- Run in the Supabase SQL editor BEFORE merging the PR; the code needs the table.
create table if not exists public.stripe_event (
    id              text primary key,                 -- Stripe event id: blocks duplicate deliveries
    type            text,
    payload         text,                             -- the verified event, so retries never depend
                                                      -- on Stripe's 30-day event retention
    api_version     text,
    status          text not null default 'pending',  -- pending|processing|processed|failed|dead
    attempts        integer not null default 0,
    last_error      text,
    claimed_at      timestamptz,                      -- lease start; a stale one may be reclaimed
    claimed_by      text,                             -- owner token; required to write the result
    next_attempt_at timestamptz default now(),
    created_at      timestamptz not null default now(),
    processed_at    timestamptz
);

-- If the table already existed from an earlier attempt, make sure the newer columns are there:
alter table public.stripe_event add column if not exists payload text;
alter table public.stripe_event add column if not exists api_version text;
alter table public.stripe_event add column if not exists claimed_at timestamptz;
alter table public.stripe_event add column if not exists claimed_by text;
alter table public.stripe_event add column if not exists next_attempt_at timestamptz default now();

-- Legacy rows from the first version of this table have no lease or due time; without these they
-- could never be claimed or reclaimed.
update public.stripe_event set next_attempt_at = coalesce(next_attempt_at, now()) where next_attempt_at is null;
update public.stripe_event set claimed_at = coalesce(claimed_at, created_at) where status = 'processing' and claimed_at is null;

create index if not exists stripe_event_retry_idx
    on public.stripe_event (status, next_attempt_at);
create index if not exists stripe_event_claimed_idx
    on public.stripe_event (status, claimed_at);

-- The API connects with the service role, which bypasses RLS. Enable RLS with no policies so the
-- anon/authenticated keys cannot read payment events.
alter table public.stripe_event enable row level security;

-- Checks to run after the deploy:
-- select status, count(*) from public.stripe_event group by status;                 -- expect processed
-- select id, type, attempts, last_error from public.stripe_event where status = 'dead';  -- expect none

-- Atomic claim. One statement, database clock, eligibility and attempt increment together, so two
-- workers can never consume the same attempt or bypass a backoff window.
create or replace function public.claim_stripe_event(
    p_event_id text, p_max_attempts integer, p_token text, p_lease_seconds integer)
returns boolean
language plpgsql
as $$
declare
    updated integer;
begin
    update public.stripe_event
       set status = 'processing',
           claimed_at = now(),
           claimed_by = p_token,
           attempts = attempts + 1
     where id = p_event_id
       and attempts < p_max_attempts
       and (
            (status in ('pending', 'failed') and coalesce(next_attempt_at, now()) <= now())
            or (status = 'processing' and claimed_at < now() - make_interval(secs => p_lease_seconds))
       );
    get diagnostics updated = row_count;
    return updated > 0;
end;
$$;

-- ---------------------------------------------------------------------------------------------
-- Order-level fulfilment lock (montyesim-eshop-api#7).
-- Ordering at the eSIM Hub is not idempotent and the hub has no lookup by our identifier, so
-- exactly one worker may ever be inside that call for a given order.
alter table public.user_order add column if not exists fulfilment_claimed_at timestamptz;

create or replace function public.claim_order_fulfilment(p_order_id text, p_lease_seconds integer)
returns boolean
language plpgsql
as $$
declare
    updated integer;
begin
    update public.user_order
       set order_status = 'fulfilling',
           fulfilment_claimed_at = now()
     where id::text = p_order_id
       and esim_order_id is null              -- already ordered at the hub: never claim again
       and (
            order_status in ('pending', 'failure')
            or (order_status = 'fulfilling'
                and fulfilment_claimed_at < now() - make_interval(secs => p_lease_seconds))
       );
    get diagnostics updated = row_count;
    return updated > 0;
end;
$$;
