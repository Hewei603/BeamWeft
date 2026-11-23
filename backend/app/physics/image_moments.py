"""Second moments of a signed, background-corrected intensity within a stated detector window."""
import math

import numpy as np

from .detector_frames import read_pixels
from .raster_overlap import aggregate_cells


def _blocks(pixels, left, top, max_side):
    x_edges, y_edges, totals = aggregate_cells(pixels, max_side)
    return x_edges + left - .5, y_edges + top - .5, totals


def _publish_map(args, pixels, result):
    if args.response not in ('linear', 'assumed_linear'):
        explanation = ("原件的响应已标注为非线性。请先用仪器响应曲线将读数还原为线性信号。" if args.response == 'nonlinear'
                       else "尚未核实读数与光强的关系。可提供线性响应依据，或选择本次暂按线性分析。")
        result['output_limits'] = {'intensity_map': explanation, 'centroid': explanation}
        result['warnings'].append(explanation + " 当前质心和直径按原件读数加权。")
        return
    if args.response == 'assumed_linear':
        result['warnings'].append("本次暂按探测器线性响应解释原件读数，光强图及宽度以此为条件。")
    if args.pitch_x_um is None or args.pitch_y_um is None:
        return
    if args.saturation_level is None:
        result['warnings'].append("未提供仪器饱和值，未检验是否有像元饱和。")
    if result['negative_pixels']:
        result['warnings'].append(f"保留了 {result['negative_pixels']} 个负读数像元，包括它们对分块积分的贡献。")
    background = result['background']
    limited = (result.get('saturated_pixels') or result.get('encoding_ceiling_pixels') or result.get('dark_saturated_pixels') or result['source'].get('response_warning')
               or background.get('source', {}).get('response_warning') or background['method'] == 'raw')
    if limited:
        quality = 'limited'
    elif (args.response == 'assumed_linear' or args.saturation_level is None or result.get('saturation_threshold_out_of_range')
          or background.get('assumed_same_units')):
        quality = 'conditional'
    else:
        quality = 'usable'
    left, top, width, height = result['source']['window']
    acquisition_warnings = list(result["warnings"])
    xs, ys, totals = _blocks(pixels, left, top, max_side=256)
    row_bin, col_bin = math.ceil(height/256), math.ceil(width/256)
    if row_bin > 1 or col_bin > 1:
        result['warnings'].append(f"强度图每块累加至多 {col_bin} 列 × {row_bin} 行原始像元，块内细节只在原件中保留。缩小像素选区可保留更细采样。")
    correction = (f"扣除暗场原件 #{args.dark.attachment_id} 第 {args.dark_frame} 帧。" if args.dark is not None else
                  f"每像元扣除均匀背景 {args.background_level:g}。" if args.background_level is not None else "未扣背景。")
    result['intensity_map'] = {
        'x_edges_mm': (xs * (args.pitch_x_um / 1000)).tolist(), 'y_edges_mm': (ys * (args.pitch_y_um / 1000)).tolist(),
        'values': totals.tolist(), 'signal_unit': '原件读数', 'x_label': '原件列方向', 'y_label': '原件行方向', 'y_down':True,
        'basis': (f"原件 #{args.image.attachment_id} 第 {args.frame} 帧，零点为整帧左上像元中心，列向右、行向下。"
                  f"两轴像元节距 {args.pitch_x_um:g} 和 {args.pitch_y_um:g} µm。矩形单元记录所含像元的校正读数之和。"
                  f"像素窗（左列、上行、宽、高）为 {left}、{top}、{width}、{height}。{correction}采集依据：{args.basis}"),
        'quality': quality, 'warnings': list(result['warnings']),
    }
    if args.plane is not None:
        result['intensity_map']['plane'] = args.plane
    if args.wavelength_nm is not None:
        result['intensity_map']['wavelength_nm'] = args.wavelength_nm
    return acquisition_warnings


def _axes(covariance):
    eigenvalues, vectors = np.linalg.eigh(covariance)
    if eigenvalues[0] < -64 * np.finfo(float).eps * max(abs(eigenvalues)):
        return None
    minor, major = np.sqrt(np.maximum(eigenvalues, 0))
    direction = vectors[:, 1]
    angle = (math.degrees(math.atan2(direction[1], direction[0])) + 90) % 180 - 90
    return {"minor":4*minor, "major":4*major, "angle_deg":None if math.isclose(major, minor, rel_tol=1e-10) else angle}


def _publish_centroid(args, result, x, y, covariance, acquisition_warnings):
    calibrated = result.get("intensity_map")
    if calibrated is None:
        return
    # The map can group many original pixels. Both moments below come from the
    # full-resolution cropped detector samples in original row/column coordinates.
    sx, sy = args.pitch_x_um / 1000, args.pitch_y_um / 1000
    center = {
        "x_mm":float(x*sx), "y_mm":float(y*sy), "pixel_center":[x,y], "pitch_um":[args.pitch_x_um,args.pitch_y_um],
        "quality":calibrated["quality"], "warnings":acquisition_warnings, "window_coverage":args.window_coverage,
        "window":result["source"]["window"], "attachment_id":args.image.attachment_id, "frame":args.frame,
        "basis":f"整帧左上像元中心为原点，横向沿列号增加、纵向沿行号增加。质心使用选区全部原始像元。{args.basis}",
        "wavelength_nm":args.wavelength_nm, "plane":args.plane,
    }
    if covariance is None:
        center["quality"] = "limited"
        center["warnings"].append("扣背景后的像素协方差与光强分布不相容，请先核对背景和选区。")
    else:
        center["covariance_mm2"] = (covariance * np.outer([sx,sy],[sx,sy])).tolist()
    result["centroid"] = center


def image_moments(args):
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            return _measure(args)
    except (FloatingPointError, OverflowError, np.linalg.LinAlgError) as exc:
        raise ValueError("像素校正或二阶矩超出数值范围，请核对原图、背景和标定尺度。") from exc


def _measure(args):
    pixels, source = read_pixels(args.image, args.frame, args.window, dtype=np.float64)
    left, top, width, height = source["window"]
    result = {"source":source, "response":args.response, "warnings":[], "status":"completed",
              "assumptions":["以左上像素中心为 (0, 0)，列向右、行向下。图中每个色块显示所含像素的均值，统计采用选区全部原始像素。",
                             "直径为强度二阶矩的 4σ；结果依赖线性响应、背景和选区。边界外的强度未被观测。"]}
    if args.saturation_level is not None:
        result["saturated_pixels"] = int(np.count_nonzero(pixels >= args.saturation_level))
        if result["saturated_pixels"]:
            result["warnings"].append(f"选区有 {result['saturated_pixels']} 个像元达到指定饱和值 {args.saturation_level:g}，强度分布、宽度与质心可能有偏。")
    encoded_max = source.get('encoded_max')
    if encoded_max is not None and (args.saturation_level is None or args.saturation_level > encoded_max):
        if args.saturation_level is not None:
            result['saturation_threshold_out_of_range'] = True
            result['warnings'].append(f"指定饱和值 {args.saturation_level:g} 超出原件编码上限 {encoded_max}，请核对仪器读数单位。")
        result['encoding_ceiling_pixels'] = int(np.count_nonzero(pixels == encoded_max))
        if result['encoding_ceiling_pixels']:
            result['warnings'].append(f"有 {result['encoding_ceiling_pixels']} 个像元达到文件编码上限 {encoded_max}，无法排除采集或导出时发生截顶。")
    if args.dark is not None:
        dark, reference = read_pixels(args.dark, args.dark_frame, args.window)
        array_input = "NPY" in (source["format"],reference["format"])
        if reference['size'] != source['size']:
            raise ValueError("暗场与信号帧的原图尺寸不同，请配准到同一像元阵列。")
        if array_input:
            if source["sample_type"] != reference["sample_type"] and not args.dark_units_note:
                raise ValueError("NPY 暗场配对使用了不同数值类型。请核对读数单位和相机增益，"
                                 "若两者已经处于相同标度，请填写暗场与信号读数单位的对应依据。")
        elif not (reference.get('encoded_max') == source['encoded_max'] if 'encoded_max' in source else
                  reference['mode'] == source['mode'] and 'encoded_max' not in reference):
            raise ValueError("暗场与信号帧的原图尺寸及像元读数范围须一致，请核对采集和配准。")
        if args.saturation_level is not None:
            result['dark_saturated_pixels'] = int(np.count_nonzero(dark >= args.saturation_level))
            if result['dark_saturated_pixels']:
                result['warnings'].append(f"暗场有 {result['dark_saturated_pixels']} 个像元达到指定饱和值，背景扣除可能有偏。")
        pixels -= dark
        del dark
        result["background"] = {"method":"dark_frame", "source":reference}
        if args.dark_units_note:
            result["background"]["unit_registration"] = args.dark_units_note
        elif array_input:
            result["background"]["assumed_same_units"] = True
            result["warnings"].append("NPY 记录了数值类型。此次暗场扣除按相同的读数单位与相机增益计算，"
                                      "可用独立的采集设置补充两帧的对应依据。")
        if reference.get("response_warning"):
            result["warnings"].append("暗场：" + reference["response_warning"])
    elif args.background_level is not None:
        pixels -= args.background_level
        result["background"] = {"method":"constant","level":args.background_level}
    else:
        result["background"] = {"method":"raw"}
        result["warnings"].append("未提供暗场或背景值。质心和宽度包含选区内的背景信号。")
    if source.get("response_warning"):
        result["warnings"].append(source["response_warning"])
    xs, ys, totals = _blocks(pixels, left, top, max_side=96)
    means = totals / np.outer(np.diff(ys), np.diff(xs))
    result["grid"] = {"x_edges":xs.tolist(), "y_edges":ys.tolist(), "means":means.tolist()}
    result["negative_pixels"] = int(np.count_nonzero(pixels < 0))
    acquisition_warnings = _publish_map(args, pixels, result)
    missing_width = 'partial' if 'intensity_map' in result else 'needs_input'
    mx, my = pixels.sum(axis=0), pixels.sum(axis=1)
    total = float(mx.sum())
    result["sum"] = total
    if total <= 0:
        message = "选区校正后的总信号非正，无法求强度质心和宽度。请检查背景或选择包含信号的区域。"
        result.setdefault("output_limits", {}).setdefault("centroid", message)
        return {**result, "status":missing_width, "message":message}
    x, y = np.arange(width) + left, np.arange(height) + top
    cx, cy = float(x @ mx / total), float(y @ my / total)
    dx, dy = x-cx, y-cy
    xx, yy, xy = (dx**2 @ mx)/total, (dy**2 @ my)/total, (dy @ (pixels @ dx))/total
    covariance = np.array([[xx,xy],[xy,yy]])
    axes = _axes(covariance)
    center_in_window = left-.5 <= cx <= left+width-.5 and top-.5 <= cy <= top+height-.5
    if center_in_window:
        result.update(center_x_px=cx, center_y_px=cy)
        _publish_centroid(args, result, cx, cy, covariance if axes is not None else None, acquisition_warnings)
    else:
        result.setdefault("output_limits", {}).setdefault("centroid", "校正后的质心位于选区外，请核对背景和选区。")
    if axes is None or not center_in_window:
        return {**result, "status":missing_width, "message":"校正后的质心或二阶矩与此选区不相容。请核对暗场、背景水平与边界。"}
    result.update(axes_px=axes, covariance_px2=covariance.tolist(),
                  diameter_x_px=4*math.sqrt(max(0,xx)), diameter_y_px=4*math.sqrt(max(0,yy)))
    angle = math.radians(axes["angle_deg"] or 0)
    t = np.linspace(0, 2*math.pi, 97)
    a, b = axes["major"]*.5*np.cos(t), axes["minor"]*.5*np.sin(t)
    result["ellipse"] = {"x":(cx + a*math.cos(angle) - b*math.sin(angle)).tolist(),
                         "y":(cy + a*math.sin(angle) + b*math.cos(angle)).tolist()}
    if args.pitch_x_um is not None:
        result['diameter_x_um'] = result['diameter_x_px'] * args.pitch_x_um
    if args.pitch_y_um is not None:
        result['diameter_y_um'] = result['diameter_y_px'] * args.pitch_y_um
    if args.pitch_x_um is not None and args.pitch_y_um is not None:
        sx, sy = args.pitch_x_um, args.pitch_y_um
        physical = _axes(covariance * np.outer([sx,sy],[sx,sy]))
        if physical is None:
            raise ValueError("标定后的协方差无法得到物理宽度，请核对像素尺度。")
        result['axes_um'] = physical
    return result
