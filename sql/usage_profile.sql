-- usage_profile.sql — what a player's role actually is.
--
-- Params:
--   $season     completed season to profile
--
-- Returns one row per player: full-season opportunity rates, the same rates over
-- the last six games he appeared in, and the delta between them. The delta is
-- the point — a 18% season target share that is 25% over the last six weeks is a
-- different investment than the reverse, and a season average hides both.
--
-- This measures OPPORTUNITY, not quality. Target share and red-zone touches are
-- a coaching decision and are reasonably stable. Efficiency is noisy. Read this
-- alongside points_over_expected.sql, which is where the regression signal lives.
--
-- Joins go through gsis_id, or through ff_playerids when a source is keyed
-- differently (snap counts are keyed on pfr_id). Never on player name: suffix
-- and collision mismatches corrupt the board silently. As of the 2025 season
-- this crosswalk resolves 624 of 631 fantasy-position snap-count players.

WITH crosswalk AS (
    SELECT gsis_id, pfr_id
    FROM ff_playerids
    WHERE gsis_id IS NOT NULL AND pfr_id IS NOT NULL
),

snaps AS (
    SELECT x.gsis_id AS player_id, s.week, s.offense_pct, s.offense_snaps
    FROM snap_counts s
    JOIN crosswalk x ON x.pfr_id = s.pfr_player_id
    WHERE s.season = $season AND s.game_type = 'REG'
),

expected AS (
    SELECT player_id, week, total_fantasy_points_diff
    FROM ff_opportunity
    WHERE season = $season
),

redzone AS (
    SELECT player_id, week, rz20_touches, rz10_touches, rz20_touchdowns
    FROM pbp_redzone
    WHERE season = $season
),

weekly AS (
    SELECT
        ps.player_id,
        ps.player_display_name              AS player,
        ps.position,
        ps.team,
        ps.week,
        ps.targets,
        ps.carries,
        ps.receptions,
        ps.target_share,
        ps.air_yards_share,
        ps.wopr,
        sn.offense_pct                      AS snap_pct,
        coalesce(rz.rz20_touches, 0)        AS rz20_touches,
        coalesce(rz.rz10_touches, 0)        AS rz10_touches,
        coalesce(rz.rz20_touchdowns, 0)     AS rz20_touchdowns,
        ex.total_fantasy_points_diff        AS poe,
        -- Recency is over games the player actually appeared in, so a player who
        -- missed a month is compared on his last six *games*, not six weeks of
        -- which three are blanks.
        row_number() OVER (PARTITION BY ps.player_id ORDER BY ps.week DESC) AS recency
    FROM player_stats ps
    LEFT JOIN snaps    sn ON sn.player_id = ps.player_id AND sn.week = ps.week
    LEFT JOIN redzone  rz ON rz.player_id = ps.player_id AND rz.week = ps.week
    LEFT JOIN expected ex ON ex.player_id = ps.player_id AND ex.week = ps.week
    WHERE ps.season = $season
      AND ps.season_type = 'REG'
      AND ps.position IN ('QB', 'RB', 'WR', 'TE')
      AND (ps.targets > 0 OR ps.carries > 0 OR ps.attempts > 0)
),

full_season AS (
    SELECT
        player_id,
        arg_max(player, week)              AS player,
        arg_max(position, week)            AS position,
        arg_max(team, week)                AS team,
        count(*)                           AS games,
        avg(snap_pct)                      AS snap_pct,
        avg(target_share)                  AS target_share,
        avg(air_yards_share)               AS air_yards_share,
        avg(wopr)                          AS wopr,
        sum(targets)                       AS targets,
        sum(carries)                       AS carries,
        sum(rz20_touches)                  AS rz20_touches,
        sum(rz10_touches)                  AS rz10_touches,
        sum(rz20_touchdowns)               AS rz20_touchdowns,
        sum(rz20_touches)  / count(*)      AS rz20_per_game,
        sum(rz10_touches)  / count(*)      AS rz10_per_game,
        sum(poe)                           AS poe
    FROM weekly
    GROUP BY player_id
),

last_six AS (
    SELECT
        player_id,
        count(*)                      AS games_last6,
        avg(snap_pct)                 AS snap_pct_last6,
        avg(target_share)             AS target_share_last6,
        avg(air_yards_share)          AS air_yards_share_last6,
        avg(wopr)                     AS wopr_last6,
        sum(rz20_touches) / count(*)  AS rz20_per_game_last6,
        sum(poe)                      AS poe_last6
    FROM weekly
    WHERE recency <= 6
    GROUP BY player_id
)

SELECT
    f.player_id,
    f.player,
    f.position,
    f.team,
    f.games,
    round(f.snap_pct, 3)             AS snap_pct,
    round(f.target_share, 4)         AS target_share,
    round(f.air_yards_share, 4)      AS air_yards_share,
    round(f.wopr, 3)                 AS wopr,
    f.targets,
    f.carries,
    f.rz20_touches,
    f.rz10_touches,
    f.rz20_touchdowns,
    round(f.rz20_per_game, 2)        AS rz20_per_game,
    round(f.rz10_per_game, 2)        AS rz10_per_game,
    round(f.poe, 1)                  AS poe,

    l.games_last6,
    round(l.snap_pct_last6, 3)       AS snap_pct_last6,
    round(l.target_share_last6, 4)   AS target_share_last6,
    round(l.wopr_last6, 3)           AS wopr_last6,
    round(l.rz20_per_game_last6, 2)  AS rz20_per_game_last6,
    round(l.poe_last6, 1)            AS poe_last6,

    round(l.snap_pct_last6     - f.snap_pct, 3)     AS snap_pct_trend,
    round(l.target_share_last6 - f.target_share, 4) AS target_share_trend,
    round(l.wopr_last6         - f.wopr, 3)         AS wopr_trend,

    -- Sample size is a required output, not a footnote. Below four games these
    -- rates describe noise, and the caller must widen confidence accordingly.
    CASE WHEN f.games >= 4 THEN 'ok' ELSE 'insufficient_data' END AS data_status,
    $season                          AS season,
    'rates averaged over games played; last6 = final 6 games the player appeared in'
                                     AS sample_note
FROM full_season f
LEFT JOIN last_six l USING (player_id)
ORDER BY f.wopr DESC NULLS LAST, f.targets DESC
