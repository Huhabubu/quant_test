package dev.doudou.client;

/** Deterministic standalone test run by Gradle's verifyWorldClock task. */
public final class WorldClockTest {
    private WorldClockTest() {}

    private static void check(long worldTime, long dayTime, String clockTime) {
        if (WorldClock.dayTime(worldTime) != dayTime
            || !WorldClock.clockTime(worldTime).equals(clockTime)) {
            throw new AssertionError("worldTime=" + worldTime
                + " expected " + dayTime + " / " + clockTime
                + " got " + WorldClock.dayTime(worldTime)
                + " / " + WorldClock.clockTime(worldTime));
        }
    }

    public static void main(String[] args) {
        check(0L, 0L, "06:00");
        check(1000L, 1000L, "07:00");
        check(6000L, 6000L, "12:00");
        check(12000L, 12000L, "18:00");
        check(13000L, 13000L, "19:00");
        check(18000L, 18000L, "00:00");
        check(23000L, 23000L, "05:00");
        check(23999L, 23999L, "05:59");
        check(24000L, 0L, "06:00");
        check(48000L + 15500L, 15500L, "21:30");
        check(-1L, 23999L, "05:59");
        System.out.println("WorldClock: 11 cases passed");
    }
}
