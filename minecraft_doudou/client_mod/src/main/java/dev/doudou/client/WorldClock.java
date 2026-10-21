package dev.doudou.client;

import java.util.Locale;

/**
 * Formats the server-synchronized Minecraft daylight clock for state.json.
 *
 * Daylight time is not the real-world wall clock or total game uptime:
 * /time set and sleeping can change it abruptly. The time is meaningful
 * for sleep diagnostics, but does not prove that a bed is usable.
 */
final class WorldClock {
    static final long TICKS_PER_DAY = 24000L;

    private WorldClock() {}

    static long dayTime(long worldTime) {
        return Math.floorMod(worldTime, TICKS_PER_DAY);
    }

    static String clockTime(long worldTime) {
        long ticks = dayTime(worldTime);
        // A Minecraft day starts at 06:00. 1000 ticks = one game-clock hour.
        long minuteOfDay = (360L + ticks * 60L / 1000L) % 1440L;
        return String.format(Locale.ROOT, "%02d:%02d",
            minuteOfDay / 60L, minuteOfDay % 60L);
    }
}
