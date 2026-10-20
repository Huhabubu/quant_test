package dev.doudou.client;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.util.ArrayList;
import java.util.Comparator;
import net.minecraft.client.Minecraft;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.Mob;
import net.minecraft.world.entity.MobCategory;
import net.minecraft.world.entity.animal.Animal;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.phys.AABB;
import net.minecraft.world.phys.Vec3;

/**
 * A bounded snapshot of client-loaded entities; NOT a wall-penetrating visual
 * confirmation, and NOT persistent memories of moving objects.
 */
final class NearbyEntities {
    static final int RADIUS = 24;
    private static final int MAX_ENTITIES = 64;

    static JsonArray scan(Minecraft mc) {
        JsonArray result = new JsonArray();
        if (mc.player == null || mc.level == null) return result;
        Vec3 origin = mc.player.position();
        AABB bounds = new AABB(origin.x - RADIUS, origin.y - RADIUS,
            origin.z - RADIUS, origin.x + RADIUS, origin.y + RADIUS, origin.z + RADIUS);
        ArrayList<Entity> found = new ArrayList<>();
        for (Entity entity : mc.level.getEntities(mc.player, bounds,
                 e -> e instanceof Player || e instanceof Mob || e instanceof ItemEntity)) {
            if (!entity.isRemoved() && entity.position().distanceToSqr(origin) <= RADIUS * RADIUS) {
                found.add(entity);
            }
        }
        found.sort(Comparator.comparingDouble(e -> e.position().distanceToSqr(origin)));
        for (int i = 0; i < Math.min(MAX_ENTITIES, found.size()); i++) {
            Entity entity = found.get(i);
            JsonObject item = new JsonObject();
            String category = entity instanceof Player ? "player"
                : entity instanceof ItemEntity ? "dropped_item"
                : entity.getType().getCategory() == MobCategory.MONSTER ? "hostile"
                : entity instanceof Animal ? "animal" : "other_mob";
            item.addProperty("category", category);
            item.addProperty("type", BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString());
            item.addProperty("name", entity.getName().getString());
            item.addProperty("x", entity.getX());
            item.addProperty("y", entity.getY());
            item.addProperty("z", entity.getZ());
            item.addProperty("distance", Math.round(
                entity.position().distanceTo(origin) * 100.0) / 100.0);
            if (entity instanceof ItemEntity drop) {
                item.addProperty("item_id",
                    BuiltInRegistries.ITEM.getKey(drop.getItem().getItem()).toString());
                item.addProperty("count", drop.getItem().getCount());
            }
            result.add(item);
        }
        return result;
    }
}
