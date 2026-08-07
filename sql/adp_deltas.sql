-- adp_deltas.sql — what the market costs, from two different markets.
--
-- Params:
--   $page_type           ff_rankings page to read, e.g. 'redraft-overall'
--   $teams               league size, used to convert an overall rank into a round
--   $exclude_positions   positions this league does not roster, e.g. ['K']
--
-- ADP and ECR are PRICES, not values. This query says where the crowd is
-- drafting a player and how much it disagrees with itself — it makes no claim
-- about whether that price is right. The edge is a board tuned to one league's
-- exact settings that is transparent about why it disagrees, not a claim of
-- beating a large crowd pricing public information.
--
-- TWO MARKETS, DELIBERATELY NOT MERGED
--
--   ECR  (ff_rankings)  where experts say a player SHOULD go. A value anchor.
--                       This is what the board prices against.
--   ADP  (sleeper_adp)  where he ACTUALLY goes on Sleeper. An availability
--                       anchor. This is what slot-survival math runs on, because
--                       Sleeper's draft board sorts by it and that ordering is
--                       what the other managers in the league are looking at.
--
-- Averaging them would destroy the only thing worth having. `ecr_vs_adp` is the
-- headline output: a player experts rank 30th but the room drafts 55th is who
-- falls to you at a discount. Both columns are labelled with their own source
-- and date because "beat ADP" and "beat ECR" are different claims.
--
-- WHY BOTH SERIES ARE RE-RANKED
--
-- The two lists cover different populations. The Sleeper file ranks ~300 players
-- including 26 kickers scattered throughout; ff_rankings carries its own K rows
-- at different spots. Differencing the raw ranks would measure how much kicker
-- contamination each list happens to carry above a given player — an error that
-- grows the further down the board you read. So each series is dense-ranked
-- AFTER the $exclude_positions filter, and only those adjusted ranks are
-- differenced.
--
-- Ordering breaks ties on player name so the row order is deterministic. Six
-- players share an ECR value in the current snapshot; without a tiebreak the
-- window ranks and the final ORDER BY disagree and two runs of the same query
-- return different orderings.
--
-- `market_spread` (worst minus best) is the useful uncertainty column: a player
-- ranked 40th with a spread of 60 is a disagreement, not a consensus, and the
-- board should widen confidence rather than average the disagreement away.
--
-- CROSSWALK. ff_rankings carries no gsis_id, so it links through
-- ff_playerids.fantasypros_id. Roughly 81% of the full list links; the top of the
-- board links near-perfectly and the misses are mostly rookies the crosswalk has
-- not picked up yet. The ADP side is resolved at ingest by
-- src/ffdraft/market/resolve.py and currently links 100%. Unlinked rows on
-- either side are RETURNED, not dropped, with crosswalk_status = 'unlinked' — a
-- player silently missing from the board is a worse failure than one flagged as
-- unjoinable.
--
-- Team defenses have no gsis_id on either side, so they join on the full team
-- name via the `teams` table — see the note on the join below for why the
-- abbreviation cannot be used. Note that the `team` column returned here stays
-- in ff_rankings' own dialect; it is for reading, never for joining.

WITH market AS (
    SELECT
        cast(r.id AS VARCHAR) AS fantasypros_id,
        r.player,
        r.pos                 AS position,
        r.tm                  AS team,
        r.bye,
        r.ecr,
        r.best,
        r.worst,
        r.sd,
        r.scrape_date
    FROM ff_rankings r
    WHERE r.page_type = $page_type
      AND r.ecr IS NOT NULL
      AND NOT list_contains($exclude_positions, r.pos)
),

-- One abbreviation per team name. The teams table carries historical franchises
-- and maps "Los Angeles Rams" to both LA and LAR; without this the DST join
-- fans out and duplicates a row. Prefer the code the warehouse actually uses —
-- rosters, team_stats and dst_stats all say LA.
team_lookup AS (
    SELECT
        team_name,
        coalesce(
            max(team_abbr) FILTER (
                WHERE team_abbr IN (SELECT DISTINCT team FROM rosters WHERE team IS NOT NULL)
            ),
            max(team_abbr)
        ) AS team_abbr
    FROM teams
    GROUP BY team_name
),

linked AS (
    SELECT
        m.*,
        pid.gsis_id,
        -- One key covering both kinds of row: players join on gsis_id, team
        -- defenses on their nflverse abbreviation. NULL when neither is
        -- available, which makes the join miss rather than collide.
        CASE
            WHEN m.position = 'DST' THEN 'DST:' || t.team_abbr
            ELSE pid.gsis_id
        END AS join_key
    FROM market m
    LEFT JOIN ff_playerids pid
           ON cast(pid.fantasypros_id AS VARCHAR) = m.fantasypros_id
    -- Defenses resolve through the full team name, NOT m.team. ff_rankings uses
    -- its own abbreviation dialect — GBP, JAC, KCC, LVR, NEP, NOS, SFO, TBB —
    -- against nflverse's GB, JAX, KC, LV, NE, NO, SF, TB. Keying on the
    -- abbreviation dropped 8 of 31 defenses, and dropped them silently, which is
    -- the whole failure mode this project is built to avoid. The full team name
    -- is spelled identically in both sources.
    LEFT JOIN team_lookup t
           ON m.position = 'DST' AND t.team_name = m.player
),

-- The ADP side, restricted to the same player universe and re-ranked over it.
adp_universe AS (
    SELECT
        CASE
            WHEN a.position = 'DST' THEN 'DST:' || a.team
            ELSE a.gsis_id
        END AS join_key,
        a.adp,
        a.adp_as_of,
        a.adp_source,
        a.crosswalk_status AS adp_crosswalk_status,
        row_number() OVER (ORDER BY a.adp, a.player) AS adp_rank_adj
    FROM sleeper_adp a
    WHERE NOT list_contains($exclude_positions, a.position)
),

ranked AS (
    SELECT
        l.*,
        row_number() OVER (ORDER BY l.ecr, l.player)                       AS ecr_rank_adj,
        row_number() OVER (PARTITION BY l.position ORDER BY l.ecr, l.player) AS market_pos_rank
    FROM linked l
)

SELECT
    r.gsis_id,
    r.player,
    r.position,
    r.team,
    r.bye,

    -- --- the value anchor ---------------------------------------------------
    r.ecr,
    r.ecr_rank_adj                                         AS market_rank,
    r.ecr_rank_adj,
    r.market_pos_rank,
    -- Which round the market takes him in THIS league's size, not a 12-team default.
    cast(ceil(r.ecr_rank_adj / cast($teams AS DOUBLE)) AS INTEGER)
                                                           AS market_round,
    r.best,
    r.worst,
    round(r.worst - r.best, 1)                             AS market_spread,
    r.sd,
    CASE
        WHEN r.sd IS NULL        THEN 'unknown'
        WHEN r.sd <= 5           THEN 'tight consensus'
        WHEN r.sd <= 15          THEN 'normal disagreement'
        ELSE 'wide disagreement — widen confidence'
    END                                                    AS market_agreement,
    r.scrape_date                                          AS market_as_of,
    'ECR (FantasyPros consensus via ffverse ff_rankings)'  AS market_source,
    CASE WHEN r.gsis_id IS NULL THEN 'unlinked' ELSE 'linked' END AS crosswalk_status,

    -- --- the availability anchor --------------------------------------------
    a.adp,
    a.adp_rank_adj,
    cast(ceil(a.adp_rank_adj / cast($teams AS DOUBLE)) AS INTEGER)
                                                           AS adp_round,
    a.adp_as_of,
    a.adp_source,

    -- --- where the two disagree ---------------------------------------------
    -- Negative: experts rank him higher than the room does, so he lasts longer
    -- than his ECR implies — a target. Positive: the room is higher, so he goes
    -- earlier than experts would pay, and reaching is how you lose him anyway.
    a.adp_rank_adj - r.ecr_rank_adj                        AS ecr_vs_adp,
    CASE
        WHEN a.adp_rank_adj IS NULL THEN 'no ADP — priced on ECR alone'
        WHEN abs(a.adp_rank_adj - r.ecr_rank_adj) < $teams THEN 'agree (under a round)'
        WHEN a.adp_rank_adj > r.ecr_rank_adj THEN
            'falls ' || cast(round((a.adp_rank_adj - r.ecr_rank_adj) / cast($teams AS DOUBLE), 1) AS VARCHAR)
            || ' rounds past ECR — experts higher than the room'
        ELSE
            'goes ' || cast(round((r.ecr_rank_adj - a.adp_rank_adj) / cast($teams AS DOUBLE), 1) AS VARCHAR)
            || ' rounds before ECR — the room higher than experts'
    END                                                    AS market_disagreement
FROM ranked r
LEFT JOIN adp_universe a ON a.join_key = r.join_key
ORDER BY r.ecr, r.player
