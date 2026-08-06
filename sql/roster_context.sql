-- roster_context.sql — is the role good, or is the player good?
--
-- Params:
--   $season          completed season the usage comes from
--   $draft_season    the season being drafted for, for depth chart and rookie flags
--
-- The separation this query exists to enable: a player's share of his own
-- team's opportunity, who else is competing for it, and where he currently sits
-- on the depth chart. A 22% target share on a team that threw 700 times is a
-- different asset from the same share on a team that threw 450.
--
-- It also carries the two columns this league's roster shape makes load-bearing:
--
--   * `contingent_role` — with only five bench spots, each worth 20% of the
--     bench, a player whose case rests on someone ahead of him getting hurt is
--     far more expensive to hold here than in a deeper league. Flagged, never
--     silently folded into a rank.
--   * `ir_eligible` — a known absence can be stashed on IR without consuming a
--     bench spot, which makes those players MORE draftable here than the
--     five-man bench implies. Derived from current roster status, and it is a
--     signal to verify with news, not a conclusion.
--
-- Depth chart comes from depth_chart_current, the single pinned snapshot, so two
-- callers cannot read two different depth charts and quietly disagree.

WITH season_usage AS (
    SELECT
        player_id,
        arg_max(player_display_name, week) AS player,
        arg_max(position, week)            AS position,
        arg_max(team, week)                AS team,
        count(*)                           AS games,
        sum(targets)                       AS targets,
        sum(carries)                       AS carries,
        sum(receiving_air_yards)           AS air_yards,
        sum(targets) + sum(carries)        AS opportunities
    FROM player_stats
    WHERE season = $season
      AND season_type = 'REG'
      AND position IN ('QB', 'RB', 'WR', 'TE')
    GROUP BY player_id
),

team_totals AS (
    SELECT
        team,
        sum(targets)   AS team_targets,
        sum(carries)   AS team_carries,
        sum(air_yards) AS team_air_yards
    FROM season_usage
    GROUP BY team
),

-- Where a player sat among his own positional group last season.
competition AS (
    SELECT
        player_id,
        team,
        position,
        row_number() OVER (PARTITION BY team, position ORDER BY opportunities DESC) AS team_pos_rank,
        count(*)     OVER (PARTITION BY team, position)                             AS team_pos_players
    FROM season_usage
),

depth AS (
    SELECT
        gsis_id,
        min(pos_rank)         AS depth_rank,
        arg_min(pos_abb, pos_rank) AS depth_position,
        arg_min(team, pos_rank)    AS depth_team,
        max(dt)               AS depth_as_of
    FROM depth_chart_current
    WHERE gsis_id IS NOT NULL
    GROUP BY gsis_id
),

roster_now AS (
    SELECT
        gsis_id,
        arg_max(full_name, week)               AS player,
        arg_max(position, week)                AS position,
        arg_max(team, week)                    AS current_team,
        arg_max(status, week)                  AS roster_status,
        arg_max(status_description_abbr, week) AS roster_status_detail,
        arg_max(years_exp, week)               AS years_exp,
        arg_max(entry_year, week)              AS entry_year
    FROM rosters
    WHERE season = $draft_season
      AND gsis_id IS NOT NULL
      AND position IN ('QB', 'RB', 'WR', 'TE')
    GROUP BY gsis_id
)

-- FULL OUTER so the result covers both directions of the mismatch: rookies are
-- on a draft-season roster with no prior usage, and unsigned veterans have usage
-- with no current roster spot. Starting from usage alone would drop every rookie
-- off the board silently, which is exactly the failure this is meant to prevent.
SELECT
    coalesce(u.player_id, r.gsis_id)               AS player_id,
    coalesce(u.player, r.player)                   AS player,
    coalesce(u.position, r.position)               AS position,
    coalesce(r.current_team, u.team)               AS team,
    u.team                                         AS team_last_season,
    coalesce(u.games, 0)                           AS games,

    u.targets,
    u.carries,
    round(u.targets  / nullif(t.team_targets, 0), 4)   AS team_target_share,
    round(u.carries  / nullif(t.team_carries, 0), 4)   AS team_carry_share,
    round(u.air_yards / nullif(t.team_air_yards, 0), 4) AS team_air_yards_share,
    t.team_targets,
    t.team_carries,

    c.team_pos_rank,
    c.team_pos_players,

    d.depth_rank,
    d.depth_position,
    d.depth_as_of,

    r.roster_status,
    r.roster_status_detail,
    r.years_exp,
    r.entry_year,

    -- A rookie has no NFL usage history. College production is measured against
    -- different opposition under different usage rules and is NOT a substitute;
    -- the honest output is a stated gap, not a substituted number.
    coalesce(r.entry_year = $draft_season, FALSE)  AS rookie,

    -- Three-valued on purpose. A missing depth-chart entry is NOT evidence of a
    -- backup role, and collapsing "unknown" into "contingent" flagged two thirds
    -- of the league and made the column useless.
    CASE
        WHEN d.depth_rank IS NULL AND coalesce(u.opportunities, 0) = 0 THEN 'unknown'
        WHEN d.depth_rank = 1 OR c.team_pos_rank = 1
             OR coalesce(u.opportunities, 0) >= 150                    THEN 'lead'
        WHEN coalesce(d.depth_rank, 99) > 1
             AND coalesce(u.opportunities, 0) < 100                    THEN 'contingent'
        ELSE 'rotational'
    END                                            AS role_status,

    -- Value that depends on someone ahead of him losing the job. With five bench
    -- spots each worth 20% of the bench — and a waiver pool this rich because
    -- only 140 players are rostered leaguewide — these cost far more to hold here
    -- than in a deeper league. Flagged, never folded silently into a rank.
    (coalesce(d.depth_rank, 99) > 1
     AND coalesce(u.opportunities, 0) < 100
     AND d.depth_rank IS NOT NULL)                 AS contingent_role,

    -- Stashable on IR without spending a bench spot, which makes these players
    -- MORE draftable here than a five-man bench implies.
    --
    -- Only the reserve list ('RES' — IR, PUP, NFI) qualifies. Retired, cut, and
    -- exempt players are also "not active" but are not stashes, and lumping them
    -- in would recommend drafting a retired player as an IR bargain. A signal to
    -- verify with news, not a conclusion: confirm the reason and the timeline.
    coalesce(r.roster_status = 'RES', FALSE)       AS ir_eligible,
    r.roster_status                                AS roster_status_raw,

    CASE
        WHEN coalesce(u.games, 0) >= 4 THEN 'ok'
        ELSE 'insufficient_data'
    END                                            AS data_status,
    $season       AS usage_season,
    $draft_season AS draft_season
FROM season_usage u
FULL OUTER JOIN roster_now  r ON r.gsis_id  = u.player_id
LEFT JOIN team_totals t ON t.team      = u.team
LEFT JOIN competition c ON c.player_id = u.player_id
LEFT JOIN depth       d ON d.gsis_id   = coalesce(u.player_id, r.gsis_id)
ORDER BY coalesce(u.opportunities, 0) DESC
