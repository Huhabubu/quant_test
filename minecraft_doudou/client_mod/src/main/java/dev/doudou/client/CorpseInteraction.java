package dev.doudou.client;

import com.google.gson.JsonObject;
import java.lang.reflect.InvocationTargetException;
import java.util.UUID;
import net.minecraft.client.Minecraft;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.network.protocol.common.custom.CustomPacketPayload;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.inventory.AbstractContainerMenu;
import net.minecraft.world.level.ClipContext;
import net.minecraft.world.phys.AABB;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.EntityHitResult;
import net.minecraft.world.phys.HitResult;
import net.minecraft.world.phys.Vec3;
import net.neoforged.neoforge.client.network.ClientPacketDistributor;

/**
 * A narrow, optional integration with henkelmax Corpse.
 * No compile-time Corpse dependency; methods are resolved only when present.
 * Both commands operate ONLY on a corpse owned by the local player.
 */
final class CorpseInteraction {
    private static final double MAX_REACH = 4.5;
    private static final String MAIN_MENU =
        "de.maxhenkel.corpse.gui.CorpseInventoryContainer";
    private static final String EXTRA_MENU =
        "de.maxhenkel.corpse.gui.CorpseAdditionalContainer";
    private static final String TRANSFER_MESSAGE =
        "de.maxhenkel.corpse.net.MessageTransferItems";

    static JsonObject error(String code) {
        JsonObject result = new JsonObject();
        result.addProperty("accepted", false);
        result.addProperty("verified", false);
        result.addProperty("reason", code);
        return result;
    }

    static JsonObject sent(String code, UUID uuid) {
        JsonObject result = new JsonObject();
        result.addProperty("accepted", true);
        result.addProperty("verified", false);
        result.addProperty("reason", code);
        result.addProperty("entity_uuid", uuid.toString());
        result.addProperty("note", "Action dispatched; verify server menu and inventory in a later state snapshot");
        return result;
    }

    private static UUID ownerOf(Entity corpse) {
        if (!NearbyEntities.isCorpse(corpse)) return null;
        try {
            Object value = corpse.getClass().getMethod("getPlayerUuid").invoke(corpse);
            return value instanceof UUID ? (UUID) value : null;
        } catch (ReflectiveOperationException | RuntimeException ex) {
            return null;
        }
    }

    static boolean ownedByLocalPlayer(Minecraft mc, Entity corpse) {
        return mc.player != null && mc.player.getUUID().equals(ownerOf(corpse));
    }

    /** One server-side corpse-entity interaction. Never clicks the screen. */
    static JsonObject open(Minecraft mc, JsonObject request) {
        if (mc.player == null || mc.level == null || mc.gameMode == null) return error("not_in_world");
        if (mc.player.isDeadOrDying()) return error("player_dead");
        if (mc.gui.screen() != null || mc.player.containerMenu != mc.player.inventoryMenu) {
            return error("close_existing_menu_first");
        }
        UUID target;
        try {
            target = UUID.fromString(request.get("entity_uuid").getAsString());
        } catch (RuntimeException ex) {
            return error("invalid_entity_uuid");
        }
        Entity corpse = null;
        for (Entity candidate : mc.level.getEntities(mc.player,
                mc.player.getBoundingBox().inflate(MAX_REACH + 1),
                e -> NearbyEntities.isCorpse(e) && target.equals(e.getUUID()))) {
            if (!candidate.isRemoved()) {
                corpse = candidate;
                break;
            }
        }
        if (corpse == null) return error("corpse_not_loaded_or_missing");
        if (!ownedByLocalPlayer(mc, corpse)) return error("corpse_not_owned_or_owner_unsynced");

        Vec3 eye = mc.player.getEyePosition();
        AABB box = corpse.getBoundingBox();
        Vec3 hit = new Vec3(
            Math.max(box.minX, Math.min(eye.x, box.maxX)),
            Math.max(box.minY, Math.min(eye.y, box.maxY)),
            Math.max(box.minZ, Math.min(eye.z, box.maxZ)));
        if (eye.distanceToSqr(hit) > MAX_REACH * MAX_REACH) return error("corpse_out_of_reach");
        // Aim to a point within the corpse collision box, without moving camera.
        // Do not interact through a blocking solid block.
        Vec3 middle = box.getCenter();
        BlockHitResult trace = mc.level.clip(new ClipContext(
            eye, middle, ClipContext.Block.COLLIDER, ClipContext.Fluid.NONE, mc.player));
        if (trace.getType() != HitResult.Type.MISS) return error("corpse_line_of_sight_blocked");

        mc.gameMode.interact(mc.player, corpse,
            new EntityHitResult(corpse, middle), InteractionHand.MAIN_HAND);
        return sent("corpse_interaction_sent", target);
    }

    private static boolean isCorpseMenu(AbstractContainerMenu menu) {
        String name = menu.getClass().getName();
        return MAIN_MENU.equals(name) || EXTRA_MENU.equals(name);
    }

    /** Read-only checks for the active server-provided (not history) menu. */
    static JsonObject menuInfo(Minecraft mc) {
        JsonObject result = new JsonObject();
        result.addProperty("corpse_menu_open", false);
        result.addProperty("corpse_menu_editable", false);
        if (mc.player == null || mc.player.containerMenu == null) return result;
        AbstractContainerMenu menu = mc.player.containerMenu;
        if (!isCorpseMenu(menu)) return result;
        result.addProperty("corpse_menu_open", true);
        result.addProperty("corpse_menu_type", menu.getClass().getSimpleName());
        try {
            Object corpse = menu.getClass().getMethod("getCorpse").invoke(menu);
            Object owner = corpse.getClass().getMethod("getPlayerUuid").invoke(corpse);
            Object uuid = corpse.getClass().getMethod("getUUID").invoke(corpse);
            boolean history = Boolean.TRUE.equals(menu.getClass().getMethod("isHistory").invoke(menu));
            boolean editable = Boolean.TRUE.equals(menu.getClass().getMethod("isEditable").invoke(menu));
            result.addProperty("corpse_menu_editable", editable && !history);
            result.addProperty("corpse_menu_history", history);
            result.addProperty("corpse_menu_owned_by_self", owner.equals(mc.player.getUUID()));
            if (uuid instanceof UUID) result.addProperty("corpse_menu_uuid", uuid.toString());
        } catch (ReflectiveOperationException | RuntimeException ignored) {
            // An unknown/incompatible menu must fail closed for transfer.
        }
        return result;
    }

    /**
     * Sends exactly the same custom payload as the official "Transfer Items"
     * button. Server checks editable ITransferrable menu and alive status.
     * This is NOT a history-screen click or local inventory mutation.
     */
    static JsonObject takeAll(Minecraft mc, JsonObject request) {
        if (mc.player == null || mc.level == null || mc.player.isDeadOrDying()) {
            return error("not_in_world_or_dead");
        }
        UUID target;
        try {
            target = UUID.fromString(request.get("entity_uuid").getAsString());
        } catch (RuntimeException ex) {
            return error("invalid_entity_uuid");
        }
        JsonObject info = menuInfo(mc);
        if (!info.get("corpse_menu_open").getAsBoolean()
            || !info.get("corpse_menu_editable").getAsBoolean()
            || !info.has("corpse_menu_owned_by_self")
            || !info.get("corpse_menu_owned_by_self").getAsBoolean()
            || !info.has("corpse_menu_uuid")
            || !target.toString().equals(info.get("corpse_menu_uuid").getAsString())) {
            return error("not_editable_owned_corpse_menu");
        }
        try {
            Class<?> cls = Class.forName(TRANSFER_MESSAGE);
            Object message = cls.getDeclaredConstructor().newInstance();
            if (!(message instanceof CustomPacketPayload payload)) {
                return error("corpse_payload_incompatible");
            }
            ClientPacketDistributor.sendToServer(payload);
            return sent("corpse_transfer_packet_sent", target);
        } catch (ReflectiveOperationException | LinkageError | RuntimeException ex) {
            return error("corpse_transfer_payload_unavailable");
        }
    }
}
