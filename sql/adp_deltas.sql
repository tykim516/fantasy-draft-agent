-- adp_deltas.sql — what the market costs.
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
-- Source is ff_rankings: FantasyPros expert consensus, redistributed free by
-- ffverse. This is ECR (where experts say a player should go), which is not the
-- same thing as ADP (where he actually goes). Label it as ECR downstream. When
-- FANTASYPROS_API_KEY is set, fantasypros_adp carries true ADP and should be
-- preferred per market.adp_order in config/sources.yml.
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
-- Crosswalk: ff_rankings carries no gsis_id, so it links through
-- ff_playerids.fantasypros_id. Roughly 81% of the full list links; the top of the
-- board links near-perfectly and the misses are mostly rookies the crosswalk has
-- not picked up yet. Unlinked rows are RETURNED, not dropped, with
-- crosswalk_status = 'unlinked' — a player silently missing from the board is a
-- worse failure than one flagged as unjoinable.

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

linked AS (
    SELECT m.*, pid.gsis_id
    FROM market m
    LEFT JOIN ff_playerids pid
           ON cast(pid.fantasypros_id AS VARCHAR) = m.fantasypros_id
)

SELECT
    gsis_id,
    player,
    position,
    team,
    bye,
    ecr,
    row_number() OVER (ORDER BY ecr, player)                       AS market_rank,
    row_number() OVER (PARTITION BY position ORDER BY ecr, player) AS market_pos_rank,
    -- Which round the market takes him in THIS league's size, not a 12-team default.
    cast(ceil(row_number() OVER (ORDER BY ecr, player) / cast($teams AS DOUBLE)) AS INTEGER)
                                                           AS market_round,
    best,
    worst,
    round(worst - best, 1)                                 AS market_spread,
    sd,
    CASE
        WHEN sd IS NULL          THEN 'unknown'
        WHEN sd <= 5             THEN 'tight consensus'
        WHEN sd <= 15            THEN 'normal disagreement'
        ELSE 'wide disagreement — widen confidence'
    END                                                    AS market_agreement,
    scrape_date                                            AS market_as_of,
    'ECR (FantasyPros consensus via ffverse ff_rankings)'  AS market_source,
    CASE WHEN gsis_id IS NULL THEN 'unlinked' ELSE 'linked' END AS crosswalk_status
FROM linked
ORDER BY ecr, player
