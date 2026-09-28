import arcpy


aprx = arcpy.mp.ArcGISProject("CURRENT")
current_map = aprx.activeMap

if current_map is None:
    raise RuntimeError("当前没有激活的地图，请先打开一个地图视图。")

# kind值: 图例文字、RGB颜色、点大小
symbol_settings = {
    "start": ("start", [255, 0, 0, 100], 8),
    "end": ("end", [0, 200, 0, 100], 8),
    "stop": ("stop", [255, 170, 0, 100], 8),
    "way": ("way", [140, 140, 140, 100], 3),
    "normal": ("normal", [140, 140, 140, 100], 3),
}

processed = []
ignored = []
failed = []

for layer in current_map.listLayers():
    layer_name = layer.longName

    try:
        if not layer.isFeatureLayer:
            ignored.append((layer_name, "不是要素图层"))
            continue
        if layer.isBroken:
            ignored.append((layer_name, "数据源已断开"))
            continue
        if not layer.supports("SYMBOLOGY"):
            ignored.append((layer_name, "不支持符号系统"))
            continue

        description = arcpy.Describe(layer)
        if getattr(description, "shapeType", None) != "Point":
            ignored.append((layer_name, "不是点图层"))
            continue

        # Read the map layer schema directly. Do not treat the layer name as a path.
        fields = {
            field.name.casefold(): field.name
            for field in description.fields
        }
        if "kind" not in fields:
            ignored.append((layer_name, "没有kind字段"))
            continue

        kind_field = fields["kind"]
        symbology = layer.symbology
        if not hasattr(symbology, "renderer"):
            ignored.append((layer_name, "没有可用的要素渲染器"))
            continue

        symbology.updateRenderer("UniqueValueRenderer")
        symbology.renderer.fields = [kind_field]
        symbology.renderer.useDefaultSymbol = True
        symbology.renderer.defaultSymbol.color = {"RGB": [100, 100, 255, 100]}
        symbology.renderer.defaultSymbol.size = 4

        for group in symbology.renderer.groups:
            for item in group.items:
                raw_value = item.values[0][0]
                if raw_value is None:
                    continue
                kind_value = str(raw_value).strip().casefold()
                if kind_value not in symbol_settings:
                    continue
                label, color, size = symbol_settings[kind_value]
                item.label = label
                item.symbol.color = {"RGB": color}
                item.symbol.size = size

        layer.symbology = symbology
        processed.append(layer_name)

    except Exception as error:
        # One unsupported layer must not stop later layers.
        failed.append((layer_name, str(error)))

print("=" * 50)
print("kind字段符号化完成")
print("成功处理：", len(processed))
print("正常忽略：", len(ignored))
print("处理失败：", len(failed))

if processed:
    print("\n成功处理的图层：")
    for name in processed:
        print("  √", name)

if failed:
    print("\n真正处理失败的图层：")
    for name, message in failed:
        print("  ×", name, "：", message)

# Uncomment to save immediately. Ctrl+S in ArcGIS Pro also works.
# aprx.save()
