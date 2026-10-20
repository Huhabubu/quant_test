package dev.doudou.client;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.UUID;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.component.DataComponents;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.inventory.Slot;
import net.minecraft.world.level.ClipContext;
import net.minecraft.world.level.block.BarrelBlock;
import net.minecraft.world.level.block.BedBlock;
import net.minecraft.world.level.block.ChestBlock;
import net.minecraft.world.level.block.ShulkerBoxBlock;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.Vec3;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.Mod;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.client.event.ClientTickEvent;

/**
 * A client-only companion mod. No server changes and no mouse event emulation.
 *
 * Python writes a NEW request file via atomic rename to commands/<uuid>.json.
 * The MC game thread claims it via rename to *.processing BEFORE execution,
 * then writes results/<uuid>.json (may be unknown after a crash; no retries).
 * Every 10 client ticks write state.json; all Minecraft objects are accessed
 * ONLY from ClientTickEvent.Post on the game thread.
 */
@Mod(value = DouDouClient.MOD_ID, dist = Dist.CLIENT)
public final class DouDouClient {
    public static final String MOD_ID = "doudou_client";

    @EventBusSubscriber(modid = MOD_ID, value = Dist.CLIENT)
    public static final class ClientEvents {
        private static Path root;
        private static int ticks;
        private static final String SESSION_ID = UUID.randomUUID().toString().replace("-", "");
        private static final int PERCEPTION_RADIUS = 12;
        private static final int PERCEPTION_VERTICAL = 6;
        private static final int PERCEPTION_MAX_TARGETS = 48;
        private static final int PERCEPTION_INTERVAL_TICKS = 40;
        private static JsonArray cachedNearby = new JsonArray();
        private static Object cachedLevel;
        private static LandmarkMemory memory;
        private static JsonArray cachedEntities = new JsonArray();
        private static long entitiesScannedAtMs;
        private static long nearbyScannedAtMs;

        @SubscribeEvent
        public static void onClientTick(ClientTickEvent.Post ignored) {
            Minecraft mc = Minecraft.getInstance();
            if (root == null) {
                root = mc.gameDirectory.toPath().resolve("config").resolve("doudou-io");
                try {
                    Files.createDirectories(root.resolve("commands"));
                    Files.createDirectories(root.resolve("results"));
                    memory = new LandmarkMemory(root);
                } catch (IOException e) {
                    return;
                }
            }

            // One command per tick prevents a flood from starving MC frames.
            runOneCommand(mc);
            if (++ticks % 10 == 0) {
                JsonObject state = snapshot(mc);
                try {
                    atomicJson(root.resolve("state.json"), state);
                } catch (IOException ignoredWriteFailure) {
                    // A missing state.json is safer than fabricating stale data.
                }
            }
        }

        private static void runOneCommand(Minecraft mc) {
            try (DirectoryStream<Path> files = Files.newDirectoryStream(root.resolve("commands"), "*.json")) {
                for (Path pending : files) {
                    String name = pending.getFileName().toString();
                    String id = name.substring(0, name.length() - ".json".length());
                    if (!id.matches("[a-f0-9]{32}")) continue;
                    Path processing = pending.resolveSibling(id + ".processing");
                    try {
                        Files.move(pending, processing, StandardCopyOption.ATOMIC_MOVE);
                    } catch (IOException movedAlready) {
                        continue;
                    }
                    JsonObject answer;
                    try {
                        JsonObject request = JsonParser.parseString(
                            Files.readString(processing, StandardCharsets.UTF_8)).getAsJsonObject();
                        long created = request.get("created_at_ms").getAsLong();
                        String session = request.get("session_id").getAsString();
                        long age = System.currentTimeMillis() - created;
                        if (!SESSION_ID.equals(session) || age < -1000 || age > 10000) {
                            answer = error("stale_or_foreign_client_session");
                        } else {
                            answer = execute(mc, request);
                        }
                    } catch (Exception ex) {
                        answer = error("invalid_request_or_game_state");
                    }
                    answer.addProperty("id", id);
                    answer.addProperty("issued_at_ms", System.currentTimeMillis());
                    try {
                        atomicJson(root.resolve("results").resolve(id + ".json"), answer);
                        Files.deleteIfExists(processing);
                    } catch (IOException missingReceipt) {
                        // *.processing remains; never repeat a potentially sent action.
                    }
                    return;
                }
            } catch (IOException ignoredReadFailure) {
                // If the directory becomes unavailable, fail closed.
            }
        }

        private static JsonObject execute(Minecraft mc, JsonObject request) {
            if (mc.player == null || mc.level == null || mc.gameMode == null) {
                return error("not_in_world");
            }
            if (mc.player.isDeadOrDying()) {
                return error("player_dead");
            }
            if (!request.has("action")) return error("missing_action");
            String action = request.get("action").getAsString();

            if ("eat".equals(action)) {
                if (mc.player.isUsingItem()) return error("already_using_item");
                if (!mc.player.getFoodData().needsFood()) return error("not_hungry");
                int slot = request.has("slot") ? request.get("slot").getAsInt() : -1;
                if (slot < -1 || slot > 8) return error("invalid_hotbar_slot");
                if (slot == -1) {
                    for (int i = 0; i < 9; i++) {
                        if (mc.player.getInventory().getItem(i).has(DataComponents.CONSUMABLE)) {
                            slot = i;
                            break;
                        }
                    }
                }
                if (slot < 0) return error("no_hotbar_food");
                ItemStack food = mc.player.getInventory().getItem(slot);
                if (food.isEmpty() || !food.has(DataComponents.CONSUMABLE)) return error("slot_not_consumable");
                mc.player.getInventory().setSelectedSlot(slot);
                // Actual protocol interaction (not a GLFW mouse event).
                mc.gameMode.useItem(mc.player, InteractionHand.MAIN_HAND);
                return dispatched("eat_sent", "Verify hunger or item count changes in a later snapshot");
            }

            if ("open_container".equals(action) || "sleep".equals(action)) {
                if (!request.has("x") || !request.has("y") || !request.has("z")) {
                    return error("missing_block_coordinates");
                }
                int x = request.get("x").getAsInt();
                int y = request.get("y").getAsInt();
                int z = request.get("z").getAsInt();
                BlockPos target = new BlockPos(x, y, z);
                Vec3 center = Vec3.atCenterOf(target);
                Vec3 eye = mc.player.getEyePosition();
                if (eye.distanceToSqr(center) > (4.5 * 4.5)) return error("block_out_of_reach");
                if (!mc.level.hasChunkAt(target)) return error("chunk_not_loaded");
                BlockState block = mc.level.getBlockState(target);
                boolean isBed = block.getBlock() instanceof BedBlock;
                boolean isBox = block.getBlock() instanceof ChestBlock
                    || block.getBlock() instanceof BarrelBlock
                    || block.getBlock() instanceof ShulkerBoxBlock;
                if ("sleep".equals(action) && !isBed) return error("not_a_bed");
                if ("open_container".equals(action) && !isBox) return error("not_a_supported_container");
                // Ray from eyes to requested block, not from camera direction.
                // Fail if another block blocks the path; no through-wall use.
                BlockHitResult visible = mc.level.clip(new ClipContext(
                    eye, center, ClipContext.Block.OUTLINE, ClipContext.Fluid.NONE, mc.player));
                if (!visible.getBlockPos().equals(target)) return error("line_of_sight_blocked");
                mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, visible);
                return dispatched("block_use_sent", "Verify the opened screen or sleeping flag later");
            }
            return error("unsupported_action");
        }

        private static JsonObject snapshot(Minecraft mc) {
            JsonObject state = new JsonObject();
            state.addProperty("schema", 1);
            state.addProperty("session_id", SESSION_ID);
            state.addProperty("captured_at_ms", System.currentTimeMillis());
            state.addProperty("in_world", mc.player != null && mc.level != null);
            if (mc.player == null || mc.level == null) {
                cachedNearby = new JsonArray();
                cachedLevel = null;
                cachedEntities = new JsonArray();
                nearbyScannedAtMs = 0;
                entitiesScannedAtMs = 0;
                return state;
            }

            state.addProperty("health", mc.player.getHealth());
            state.addProperty("max_health", mc.player.getMaxHealth());
            state.addProperty("hunger", mc.player.getFoodData().getFoodLevel());
            state.addProperty("dead", mc.player.isDeadOrDying());
            state.addProperty("sleeping", mc.player.isSleeping());
            state.addProperty("using_item", mc.player.isUsingItem());
            state.addProperty("x", mc.player.getX());
            state.addProperty("y", mc.player.getY());
            state.addProperty("z", mc.player.getZ());
            state.addProperty("selected_hotbar_slot", mc.player.getInventory().getSelectedSlot());

            JsonArray inventory = new JsonArray();
            int length = mc.player.getInventory().getContainerSize();
            for (int i = 0; i < length; i++) {
                ItemStack stack = mc.player.getInventory().getItem(i);
                if (!stack.isEmpty()) inventory.add(stackJson(stack, i));
            }
            state.add("inventory", inventory);

            JsonArray opened = new JsonArray();
            boolean containerOpened = mc.player.containerMenu != mc.player.inventoryMenu;
            state.addProperty("container_open", containerOpened);
            if (containerOpened) {
                for (int i = 0; i < mc.player.containerMenu.slots.size(); i++) {
                    Slot slot = mc.player.containerMenu.slots.get(i);
                    if (slot.hasItem()) opened.add(stackJson(slot.getItem(), i));
                }
            }
            state.add("screen_items", opened);
            state.addProperty("screen", mc.gui.screen() == null ? "" : mc.gui.screen().getClass().getSimpleName());
            boolean worldChanged = cachedLevel != mc.level;
            if (worldChanged || nearbyScannedAtMs == 0
                || ticks % PERCEPTION_INTERVAL_TICKS == 0) {
                cachedNearby = nearbyInteractables(mc);
                cachedLevel = mc.level;
                nearbyScannedAtMs = System.currentTimeMillis();
            }
            state.addProperty("nearby_scan_radius", PERCEPTION_RADIUS);
            state.addProperty("nearby_scan_vertical_range", PERCEPTION_VERTICAL);
            state.addProperty("nearby_scan_updated_at_ms", nearbyScannedAtMs);
            state.add("nearby_interactables", cachedNearby);
            String scope = LandmarkMemory.scope(mc);
            state.addProperty("landmark_memory_available", scope != null && memory != null);
            state.addProperty("dimension", LandmarkMemory.dimension(mc));
            state.add("remembered_landmarks", memory == null
                ? new JsonArray() : memory.recalled(scope, nearbyScannedAtMs, 256));
            if (worldChanged || entitiesScannedAtMs == 0 || ticks % 20 == 0) {
                cachedEntities = NearbyEntities.scan(mc);
                entitiesScannedAtMs = System.currentTimeMillis();
            }
            state.addProperty("nearby_entity_radius", NearbyEntities.RADIUS);
            state.addProperty("nearby_entities_updated_at_ms", entitiesScannedAtMs);
            state.add("nearby_entities", cachedEntities);
            return state;
        }

        /**
         * Discovery only: scan ALREADY LOADED blocks within a 12-block sphere,
         * limited to +/-6 vertical blocks. Action reach remains exactly 4.5.
         *
         * Run every 40 client ticks, not with every 10-tick state snapshot.
         * Sort all candidates before limiting to 48, so scan order cannot
         * hide the nearest chest/bed behind more distant entries.
         *
         * usable_now is a snapshot hint, NOT permission to interact:
         * execute() rechecks reach and line-of-sight at the time of use.
         */
        private static JsonArray nearbyInteractables(Minecraft mc) {
            ArrayList<JsonObject> found = new ArrayList<>();
            BlockPos base = mc.player.blockPosition();
            Vec3 eye = mc.player.getEyePosition();
            String scope = LandmarkMemory.scope(mc);
            String dimension = LandmarkMemory.dimension(mc);
            long seenAt = System.currentTimeMillis();
            final double rangeSquared = PERCEPTION_RADIUS * PERCEPTION_RADIUS;
            for (int dx = -PERCEPTION_RADIUS; dx <= PERCEPTION_RADIUS; dx++) {
                for (int dy = -PERCEPTION_VERTICAL; dy <= PERCEPTION_VERTICAL; dy++) {
                    for (int dz = -PERCEPTION_RADIUS; dz <= PERCEPTION_RADIUS; dz++) {
                        BlockPos pos = base.offset(dx, dy, dz);
                        Vec3 center = Vec3.atCenterOf(pos);
                        double distSquared = eye.distanceToSqr(center);
                        if (distSquared > rangeSquared) continue;
                        if (!mc.level.hasChunkAt(pos)) continue;
                        BlockState state = mc.level.getBlockState(pos);
                        String blockId = BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString();
                        boolean bed = state.getBlock() instanceof BedBlock;
                        boolean box = state.getBlock() instanceof ChestBlock
                            || state.getBlock() instanceof BarrelBlock
                            || state.getBlock() instanceof ShulkerBoxBlock;
                        boolean workstation = LandmarkMemory.isWorkstation(blockId);
                        if (!bed && !box && !workstation) {
                            if (memory != null) memory.forgetIfMissing(scope, pos);
                            continue;
                        }
                        String kind = bed ? "bed" : box ? "container" : "workstation";
                        if (memory != null) {
                            memory.observe(scope, dimension, pos, blockId, kind, seenAt);
                        }
                        double distance = Math.sqrt(distSquared);
                        boolean inReach = distance <= 4.5;
                        boolean visible = false;
                        if (inReach) {
                            BlockHitResult hit = mc.level.clip(new ClipContext(
                                eye, center, ClipContext.Block.OUTLINE,
                                ClipContext.Fluid.NONE, mc.player));
                            visible = hit.getBlockPos().equals(pos);
                        }
                        JsonObject block = new JsonObject();
                        block.addProperty("x", pos.getX());
                        block.addProperty("y", pos.getY());
                        block.addProperty("z", pos.getZ());
                        block.addProperty("block_id", blockId);
                        block.addProperty("kind", kind);
                        block.addProperty("distance", Math.round(distance * 100.0) / 100.0);
                        block.addProperty("usable_now", !workstation && inReach && visible);
                        found.add(block);
                    }
                }
            }
            if (memory != null) memory.flushIfNeeded(seenAt);
            found.sort(Comparator.comparingDouble(block -> block.get("distance").getAsDouble()));
            JsonArray result = new JsonArray();
            for (int i = 0; i < Math.min(found.size(), PERCEPTION_MAX_TARGETS); i++) {
                result.add(found.get(i));
            }
            return result;
        }

        private static JsonObject stackJson(ItemStack stack, int slot) {
            JsonObject item = new JsonObject();
            item.addProperty("slot", slot);
            item.addProperty("id", BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
            item.addProperty("name", stack.getHoverName().getString());
            item.addProperty("count", stack.getCount());
            item.addProperty("damage", stack.getDamageValue());
            item.addProperty("max_damage", stack.getMaxDamage());
            item.addProperty("remaining_durability", stack.isDamageableItem()
                ? Math.max(0, stack.getMaxDamage() - stack.getDamageValue()) : -1);
            return item;
        }

        private static JsonObject error(String reason) {
            JsonObject response = new JsonObject();
            response.addProperty("accepted", false);
            response.addProperty("reason", reason);
            return response;
        }

        private static JsonObject dispatched(String reason, String note) {
            JsonObject response = new JsonObject();
            response.addProperty("accepted", true);
            response.addProperty("verified", false);
            response.addProperty("reason", reason);
            response.addProperty("note", note);
            return response;
        }

        private static void atomicJson(Path destination, JsonObject json) throws IOException {
            Path temp = destination.resolveSibling("." + UUID.randomUUID() + ".tmp");
            Files.writeString(temp, json.toString(), StandardCharsets.UTF_8,
                StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE);
            try {
                Files.move(temp, destination,
                    StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            } catch (AtomicMoveNotSupportedException e) {
                Files.move(temp, destination, StandardCopyOption.REPLACE_EXISTING);
            }
        }
    }
}
