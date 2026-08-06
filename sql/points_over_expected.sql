-- points_over_expected.sql — the regression signal.
--
-- Params:
--   $season      completed season
--   $min_games   drop players below this many games (rates on 2 games are noise)
--
-- Actual minus expected fantasy points, from nflverse's ff_opportunity model.
-- This is the cleanest free separator of "scored a lot" from "will score a lot
-- again": expected points are computed from the situations a player was actually
-- put in — target depth, down and distance, field position — so the residual is
-- what he did *relative to that opportunity*.
--
-- Read the sign the counter-intuitive way. A large POSITIVE residual is usually
-- a warning, not a recommendation: it means touchdown conversion above what the
-- opportunity implied, which regresses. A negative residual on strong usage is
-- the buy-low case.
--
-- Scoring basis: ff_opportunity's fantasy points are full PPR, which was verified
-- against player_stats.fantasy_points_ppr and matches exactly. This league is
-- full PPR, so these residuals are on this league's scale for skill players.
-- They do NOT include this league's fumble-recovery-TD or special-teams-TD
-- settings, which are immaterial at this level of precision but are a real
-- (small) gap and should be stated rather than papered over.

SELECT
    o.player_id,
    arg_max(ps.player_display_name, o.week)          AS player,
    arg_max(ps.position, o.week)                     AS position,
    arg_max(ps.team, o.week)                         AS team,
    count(*)                                         AS games,

    round(sum(o.total_fantasy_points), 1)            AS actual_points,
    round(sum(o.total_fantasy_points_exp), 1)        AS expected_points,
    round(sum(o.total_fantasy_points_diff), 1)       AS poe,
    round(sum(o.total_fantasy_points_diff) / count(*), 2) AS poe_per_game,
    round(sum(o.total_fantasy_points_exp) / count(*), 2)  AS expected_per_game,

    -- Component residuals, so a reader can see WHERE the gap came from. A player
    -- running hot on touchdowns is a different case from one earning more yards
    -- per opportunity than the model expected.
    round(sum(o.rec_touchdown_diff), 2)              AS rec_td_over_expected,
    round(sum(o.rush_touchdown_diff), 2)             AS rush_td_over_expected,
    round(sum(o.receptions_diff), 1)                 AS receptions_over_expected,
    round(sum(o.rec_yards_gained_diff), 1)           AS rec_yards_over_expected,
    round(sum(o.rush_yards_gained_diff), 1)          AS rush_yards_over_expected,

    CASE
        WHEN sum(o.total_fantasy_points_diff) / count(*) >  3 THEN 'ran hot — expect regression down'
        WHEN sum(o.total_fantasy_points_diff) / count(*) < -3 THEN 'ran cold — buy-low candidate'
        ELSE 'in line with opportunity'
    END                                              AS read,

    CASE WHEN count(*) >= 8 THEN 'ok' ELSE 'small_sample' END AS data_status,
    $season                                          AS season
FROM ff_opportunity o
JOIN player_stats ps
  ON ps.player_id = o.player_id
 AND ps.season    = o.season
 AND ps.week      = o.week
WHERE o.season = $season
  AND ps.season_type = 'REG'
  AND ps.position IN ('QB', 'RB', 'WR', 'TE')
GROUP BY o.player_id
HAVING count(*) >= $min_games
ORDER BY poe DESC
