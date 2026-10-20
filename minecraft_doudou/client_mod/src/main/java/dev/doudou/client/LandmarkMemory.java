package dev.doudou.client;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;

/** Durable, bounded memories of fixed landmarks, NEVER assumed to remain valid. */
final class LandmarkMemory {
    private static final int MAX_RECORDS = 1024;
    private static final long REFRESH_DISK_MS = 30_000;
    private static final Set<String> WORKSTATIONS = Set.of(
        "minecraft:crafting_table", "minecraft:furnace", "minecraft:blast_furnace",
        "minecraft:smoker", "minecraft:smithing_table", "minecraft:stonecutter",
        "minecraft:grindstone", "minecraft:brewing_stand", "minecraft:enchanting_table",
        "minecraft:cartography_table", "minecraft:loom", "minecraft:fletching_table",
        "minecraft:anvil", "minecraft:chipped_anvil", "minecraft:damaged_anvil"
    );

    private final Path path;
    private final Map<String, Entry> entries = new HashMap<>();
    private boolean urgentDirty;
    private boolean dirty;
    private long lastSaved;

    private record Entry(String scope, String dimension, int x, int y, int z,
                         String blockId, String kind, long lastSeen) {
        String key() { return LandmarkMemory.key(scope, x, y, z); }
    }

    LandmarkMemory(Path root) {
        path = root.resolve("landmarks.json");
        load();
    }

    static boolean isWorkstation(String blockId) {
        return WORKSTATIONS.contains(blockId);
    }

    static String dimension(Minecraft mc) {
        return mc.level == null ? null : mc.level.dimension().toString();
    }

    static String scope(Minecraft mc) {
        if (mc.level == null || mc.getCurrentServer() == null
            || mc.getCurrentServer().ip == null || mc.getCurrentServer().ip.isBlank()) {
            // Do not mix unrelated offline / singleplayer worlds.
            return null;
        }
        String identity = mc.getCurrentServer().ip.trim().toLowerCase()
            + "|" + dimension(mc);
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                .digest(identity.getBytes(StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(digest);
        } catch (NoSuchAlgorithmException impossible) {
            return null;
        }
    }

    private static String key(String scope, int x, int y, int z) {
        return scope + "|" + x + "|" + y + "|" + z;
    }

    void observe(String scope, String dim, BlockPos pos, String blockId, String kind, long now) {
        if (scope == null) return;
        String key = key(scope, pos.getX(), pos.getY(), pos.getZ());
        Entry before = entries.get(key);
        if (before == null || !before.blockId.equals(blockId) || !before.kind.equals(kind)) {
            urgentDirty = true;
        }
        if (before == null || before.lastSeen != now) {
            entries.put(key, new Entry(scope, dim, pos.getX(), pos.getY(), pos.getZ(),
                blockId, kind, now));
            dirty = true;
        }
    }

    void forgetIfMissing(String scope, BlockPos pos) {
        if (scope == null) return;
        if (entries.remove(key(scope, pos.getX(), pos.getY(), pos.getZ())) != null) {
            urgentDirty = true;
            dirty = true;
        }
    }

    JsonArray recalled(String scope, long lastScan, int maxResults) {
        JsonArray result = new JsonArray();
        if (scope == null) return result;
        ArrayList<Entry> found = new ArrayList<>();
        for (Entry entry : entries.values()) if (scope.equals(entry.scope)) found.add(entry);
        found.sort(Comparator.comparingLong(Entry::lastSeen).reversed());
        for (int i = 0; i < Math.min(maxResults, found.size()); i++) {
            Entry entry = found.get(i);
            JsonObject item = serialize(entry);
            item.addProperty("seen_in_latest_scan", lastScan != 0 && entry.lastSeen == lastScan);
            item.addProperty("verification", "last_observed_not_guaranteed_current");
            result.add(item);
        }
        return result;
    }

    private static JsonObject serialize(Entry entry) {
        JsonObject item = new JsonObject();
        item.addProperty("scope", entry.scope);
        item.addProperty("dimension", entry.dimension);
        item.addProperty("x", entry.x);
        item.addProperty("y", entry.y);
        item.addProperty("z", entry.z);
        item.addProperty("block_id", entry.blockId);
        item.addProperty("kind", entry.kind);
        item.addProperty("last_seen_at_ms", entry.lastSeen);
        return item;
    }

    void flushIfNeeded(long now) {
        if (!dirty || (!urgentDirty && now - lastSaved < REFRESH_DISK_MS)) return;
        while (entries.size() > MAX_RECORDS) {
            Entry oldest = entries.values().stream().min(
                Comparator.comparingLong(Entry::lastSeen)).orElse(null);
            if (oldest == null) break;
            entries.remove(oldest.key());
        }
        JsonObject file = new JsonObject();
        file.addProperty("schema", 1);
        JsonArray records = new JsonArray();
        entries.values().stream().sorted(Comparator.comparingLong(Entry::lastSeen))
            .forEach(entry -> records.add(serialize(entry)));
        file.add("entries", records);
        try {
            Path temporary = path.resolveSibling("." + UUID.randomUUID() + ".tmp");
            Files.writeString(temporary, file.toString(), StandardCharsets.UTF_8,
                StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE);
            try {
                Files.move(temporary, path, StandardCopyOption.ATOMIC_MOVE,
                    StandardCopyOption.REPLACE_EXISTING);
            } catch (AtomicMoveNotSupportedException e) {
                Files.move(temporary, path, StandardCopyOption.REPLACE_EXISTING);
            }
            lastSaved = now;
            urgentDirty = false;
            dirty = false;
        } catch (IOException failure) {
            // Retry on the next scan. Never claim memory was durably written.
        }
    }

    private void load() {
        try {
            if (!Files.isRegularFile(path) || Files.size(path) > 1_000_000) return;
            JsonObject file = JsonParser.parseString(
                Files.readString(path, StandardCharsets.UTF_8)).getAsJsonObject();
            if (file.get("schema").getAsInt() != 1) return;
            JsonArray saved = file.getAsJsonArray("entries");
            if (saved == null || saved.size() > MAX_RECORDS) return;
            for (var json : saved) {
                try {
                    JsonObject obj = json.getAsJsonObject();
                    String scope = obj.get("scope").getAsString();
                    String dim = obj.get("dimension").getAsString();
                    int x = obj.get("x").getAsInt(), y = obj.get("y").getAsInt(),
                        z = obj.get("z").getAsInt();
                    String id = obj.get("block_id").getAsString();
                    String kind = obj.get("kind").getAsString();
                    long at = obj.get("last_seen_at_ms").getAsLong();
                    if (!scope.matches("[a-f0-9]{64}") || at <= 0
                        || !Set.of("bed", "container", "workstation").contains(kind)
                        || Math.abs(x) > 30_000_000 || Math.abs(z) > 30_000_000
                        || Math.abs((long) y) > 2048) continue;
                    Entry entry = new Entry(scope, dim, x, y, z, id, kind, at);
                    entries.put(entry.key(), entry);
                } catch (RuntimeException badRecord) {
                    // Skip a damaged item rather than losing all prior memories.
                }
            }
        } catch (IOException | RuntimeException unreadable) {
            // First boot, or corrupt local cache: fail empty, never fabricate.
        }
    }
}
