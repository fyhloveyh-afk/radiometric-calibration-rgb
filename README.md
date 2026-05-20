RGB 可见光相机辐射标定脚本使用说明
====================================

目录位置：
E:\标定

主要脚本：
radiometric_calibration_rgb.py

该脚本用于建立 RGB 相机三通道的辐射标定关系：

    L_eff_R = f_R((DN_R - Dark_R) / t)
    L_eff_G = f_G((DN_G - Dark_G) / t)
    L_eff_B = f_B((DN_B - Dark_B) / t)

其中：

    DN_R/G/B       有光时图像 ROI 平均灰度
    Dark_R/G/B     相同曝光时间下的暗场 ROI 平均灰度
    t              曝光时间，单位为秒
    L_eff_R/G/B    R/G/B 通道等效辐亮度或通道积分辐射

注意：这里的 L_eff 不是单波长辐射，而是经过相机光谱响应和 650 nm 截止滤光片加权后的通道等效辐射。


一、标定前相机设置
------------------

标定时建议使用 RAW 或线性 RGB 图像。需要关闭或固定：

    自动曝光 AE
    自动增益 AGC
    自动白平衡 AWB
    Gamma
    HDR
    降噪
    锐化
    色彩增强
    自动对比度

需要固定：

    镜头
    光圈
    焦距
    物距
    增益
    滤光片
    图像格式
    ROI 位置和大小


二、需要准备的数据
------------------

1. 相机 RGB 光谱响应文件

目前使用的文件示例：

    \\wsl.localhost\Ubuntu-22.04\home\yuhao\数据模拟\spectral_response_full.csv

文件至少需要包含以下列：

    wavelength_nm,red,green,blue

当前文件还包含 mono 列，脚本不会使用 mono 列。

示例：

    wavelength_nm,mono,green,blue,red
    300.0000,0.0000,0.0000,0.0000,0.0000
    301.0000,0.0000,0.0000,0.0000,0.0000
    ...


2. 测量数据文件

模板文件：

    calibration_measurements_template.csv
    blackbody_measurements_template.csv

测量数据有三种填写方式。


方式 A：直接填写三通道参考等效辐亮度
-------------------------------------

如果你已经提前算好了每个亮度等级对应的：

    l_eff_r
    l_eff_g
    l_eff_b

则测量 CSV 使用下面格式：

    level,exposure_ms,dn_r,dn_g,dn_b,dark_r,dark_g,dark_b,l_eff_r,l_eff_g,l_eff_b
    L1,1.0,1200,1500,900,64,62,65,1.23,1.18,0.95
    L1,2.0,2335,2938,1735,65,63,66,1.23,1.18,0.95
    L2,1.0,2100,2600,1600,64,62,65,2.15,2.04,1.66

字段说明：

    level        亮度等级名称，可自定义，例如 L1、L2、L3
    exposure_ms  曝光时间，单位 ms
    dn_r         有光图像 R 通道 ROI 平均灰度
    dn_g         有光图像 G 通道 ROI 平均灰度
    dn_b         有光图像 B 通道 ROI 平均灰度
    dark_r       暗场图像 R 通道 ROI 平均灰度
    dark_g       暗场图像 G 通道 ROI 平均灰度
    dark_b       暗场图像 B 通道 ROI 平均灰度
    l_eff_r      R 通道参考等效辐亮度
    l_eff_g      G 通道参考等效辐亮度
    l_eff_b      B 通道参考等效辐亮度

同一个 level 可以有多个曝光时间。相同 level 的 l_eff_r/g/b 应保持一致。


方式 B：填写光谱辐亮度文件，让脚本自动积分
------------------------------------------

如果你有每个亮度等级的光谱辐亮度数据，可以不填 l_eff_r/g/b，而是填 spectrum_file。

测量 CSV 格式：

    level,exposure_ms,dn_r,dn_g,dn_b,dark_r,dark_g,dark_b,spectrum_file
    L1,1.0,1200,1500,900,64,62,65,spectra/L1.csv
    L1,2.0,2335,2938,1735,65,63,66,spectra/L1.csv
    L2,1.0,2100,2600,1600,64,62,65,spectra/L2.csv

其中 spectrum_file 可以是绝对路径，也可以是相对路径。

如果是相对路径，脚本会以测量 CSV 所在目录为基准查找。

每个 spectrum_file 文件格式如下：

    wavelength_nm,radiance
    380,0.10
    400,0.12
    450,0.18
    500,0.22
    550,0.24
    600,0.20
    650,0.14
    700,0.05

字段说明：

    wavelength_nm  波长，单位 nm
    radiance       光谱辐亮度，单位由你的标准仪器决定

如果 radiance 的单位是 W/(m2 sr nm)，那么输出的 L_eff 也会对应这个单位体系。


方式 C：填写黑体炉温度，让脚本按普朗克定律自动计算
--------------------------------------------------

如果标定源是黑体炉，并且你知道每个标定点的黑体温度，可以直接填写黑体温度。

测量 CSV 格式：

    level,blackbody_temp_c,emissivity,exposure_ms,dn_r,dn_g,dn_b,dark_r,dark_g,dark_b
    T900,900,1.0,1.0,900,650,260,64,62,65
    T900,900,1.0,2.0,1740,1250,455,65,63,66
    T1000,1000,1.0,1.0,1600,1050,380,64,62,65

字段说明：

    blackbody_temp_c  黑体温度，单位摄氏度
    emissivity        发射率，理想黑体填 1.0

也可以使用开尔文温度列：

    blackbody_temp_k

如果同时存在 blackbody_temp_c 和 blackbody_temp_k，脚本优先使用 blackbody_temp_k。

脚本内部使用普朗克定律计算光谱辐亮度：

    L_lambda(lambda, T) =
        2 h c^2 / lambda^5
        /
        [ exp(h c / (lambda k T)) - 1 ]

脚本输出的光谱辐亮度单位为：

    W/(m2 sr nm)

然后再计算每个通道的等效参考辐射：

    L_eff_c =
        integral( L_lambda(lambda, T) * S_eff_c(lambda) d_lambda )
        /
        integral( S_eff_c(lambda) d_lambda )

其中：

    S_eff_c(lambda) = S_c(lambda) * T_filter(lambda)

也就是说，黑体温度会先被转换成随波长变化的黑体光谱辐亮度，再经过 R/G/B 通道光谱响应和 650 nm 滤光片加权。


方式 C 辅助：从标定图片文件夹自动生成黑体测量 CSV
------------------------------------------------

如果每个相机的标定图片已经按文件夹整理，可以先用：

    images_to_blackbody_measurements.py

把 RAW 图片的 ROI 均值转换成黑体测量 CSV。

亮场 RAW 文件命名规则：

    黑体温度摄氏度_曝光时间ms_第几次.raw

示例：

    900_100_1.raw
    900_100_2.raw
    900_100_3.raw
    1000_100_1.raw

同名 BMP 文件可以放在同一目录中，用来人工判断 ROI；转换脚本默认只读取指定后缀的 RAW 文件，不读取 BMP。

暗场建议单独放一个文件夹，并按相同曝光时间拍摄，命名规则：

    dark_曝光时间ms_第几次.raw

示例：

    dark_100_1.raw
    dark_100_2.raw
    dark_100_3.raw

运行示例：

    python .\images_to_blackbody_measurements.py `
      --image-dir .\camera01_blackbody_images `
      --dark-dir .\camera01_dark_images `
      --output-csv .\camera01_blackbody_measurements.csv `
      --roi 420,310,120,120 `
      --width 1920 `
      --height 1080 `
      --dtype uint16 `
      --channels 3 `
      --channel-order rgb `
      --raw-ext .raw

其中：

    --roi x,y,width,height
        ROI 坐标，按 BMP 图像中看到的像素坐标填写。

    --width / --height
        RAW 图像宽高。RAW 文件通常不自带尺寸信息，必须手动指定。

    --dtype
        RAW 单个通道的数据类型，常见为 uint8 或 uint16。

    --channels
        每个像素的通道数。如果 RAW 是打包 RGB，一般为 3。

    --channel-order
        RAW 通道顺序。常见为 rgb 或 bgr。

输出 CSV 会包含：

    level,blackbody_temp_c,emissivity,exposure_ms,dn_r,dn_g,dn_b,dark_r,dark_g,dark_b,repeat_count

其中 dn_r/g/b 是同一温度、同一曝光时间下多次拍摄的 ROI 平均值再求平均。
repeat_count 只是辅助检查列，主标定脚本会忽略它。

如果暂时没有暗场文件，也可以临时使用：

    --use-zero-dark

但正式标定不建议这样做。更推荐每个曝光时间都拍对应暗场。


三、650 nm 截止滤光片处理方式
-----------------------------

如果没有滤光片透过率曲线，脚本默认使用理想 650 nm 短波通过模型：

    T_filter(lambda) = 1, lambda <= 650 nm
    T_filter(lambda) = 0, lambda > 650 nm

运行时通过参数指定：

    --cutoff-nm 650

如果你有真实滤光片透过率曲线，可以准备一个 CSV：

    wavelength_nm,transmission
    400,0.92
    500,0.91
    600,0.89
    650,0.50
    700,0.01

然后运行时使用：

    --filter-csv filter_transmission.csv

脚本会用真实透过率曲线代替理想截止模型。


四、运行命令
------------

1. 使用直接参考值 l_eff_r/g/b 标定

在 PowerShell 中进入目录：

    cd E:\标定

运行：

    python .\radiometric_calibration_rgb.py `
      --measurements .\calibration_measurements_template.csv `
      --spectral-response "\\wsl.localhost\Ubuntu-22.04\home\yuhao\数据模拟\spectral_response_full.csv" `
      --cutoff-nm 650 `
      --model linear `
      --output-dir .\calibration_output


2. 使用光谱辐亮度 spectrum_file 自动积分

测量表中填写 spectrum_file 后运行：

    python .\radiometric_calibration_rgb.py `
      --measurements .\measurements_with_spectrum.csv `
      --spectral-response "\\wsl.localhost\Ubuntu-22.04\home\yuhao\数据模拟\spectral_response_full.csv" `
      --cutoff-nm 650 `
      --reference-kind normalized `
      --model linear `
      --output-dir .\calibration_output


3. 使用黑体炉温度自动按普朗克定律计算

测量表中填写 blackbody_temp_c 或 blackbody_temp_k 后运行：

    python .\radiometric_calibration_rgb.py `
      --measurements .\blackbody_measurements_template.csv `
      --spectral-response "\\wsl.localhost\Ubuntu-22.04\home\yuhao\数据模拟\spectral_response_full.csv" `
      --cutoff-nm 650 `
      --reference-kind normalized `
      --model linear `
      --output-dir .\calibration_output_blackbody


3.1 从 RAW 图片文件夹直接生成测量表并完成黑体标定

如果亮场 RAW 图片已经按“黑体温度_曝光时间_第几次”命名，可以不手动准备
measurements CSV，直接运行：

    python .\radiometric_calibration_rgb.py `
      --image-dir .\camera01_blackbody_images `
      --dark-dir .\camera01_dark_images `
      --roi 420,310,120,120 `
      --raw-width 1920 `
      --raw-height 1080 `
      --raw-dtype uint16 `
      --raw-channels 3 `
      --raw-channel-order rgb `
      --raw-ext .raw `
      --spectral-response "\\wsl.localhost\Ubuntu-22.04\home\yuhao\数据模拟\spectral_response_full.csv" `
      --cutoff-nm 650 `
      --reference-kind normalized `
      --model linear `
      --output-dir .\calibration_output_camera01

脚本会在输出目录中同时保存：

    generated_blackbody_measurements.csv

这份 CSV 是从 RAW ROI 均值自动整理出来的中间测量表，方便复查。


4. 使用真实滤光片透过率曲线

    python .\radiometric_calibration_rgb.py `
      --measurements .\measurements_with_spectrum.csv `
      --spectral-response "\\wsl.localhost\Ubuntu-22.04\home\yuhao\数据模拟\spectral_response_full.csv" `
      --filter-csv .\filter_transmission.csv `
      --reference-kind normalized `
      --model linear `
      --output-dir .\calibration_output


五、拟合模型选择
----------------

默认推荐线性模型：

    --model linear

对应：

    L_eff_c = a_c * X_c + b_c

其中：

    X_c = (DN_c - Dark_c) / exposure_s

如果相机响应存在轻微非线性，可以使用多项式：

    --model poly --degree 2

对应：

    L_eff_c = a_c * X_c^2 + b_c * X_c + d_c

如果动态范围很大，也可以使用对数多项式：

    --model logpoly --degree 2

对应：

    ln(L_eff_c) = a0 + a1 * ln(X_c) + a2 * ln(X_c)^2

建议优先使用 linear。只有在线性拟合残差明显有规律时，再考虑 poly 或 logpoly。


六、reference-kind 的含义
-------------------------

当使用 spectrum_file 自动积分时，需要选择参考辐射的定义。

默认：

    --reference-kind normalized

计算：

    L_eff_c =
        integral( L_lambda(lambda) * S_eff_c(lambda) d_lambda )
        /
        integral( S_eff_c(lambda) d_lambda )

这表示通道响应加权后的等效平均辐亮度。

如果使用：

    --reference-kind integrated

计算：

    E_c = integral( L_lambda(lambda) * S_eff_c(lambda) d_lambda )

这表示通道积分辐射。

两种方式都可以，但一次标定中必须保持一致。推荐使用 normalized。


七、输出文件说明
----------------

脚本运行后会在 output-dir 中生成：

    effective_response.csv
        乘入滤光片后的 R/G/B 等效响应数据。

    effective_response.png
        R/G/B 等效响应曲线图。

    calibration_fit_table.csv
        实际用于拟合的数据表，包含扣暗场和除曝光时间后的 X_R/G/B。

    calibration_coefficients.json
        三通道标定系数，适合后续程序读取。

    calibration_report.md
        标定公式和误差指标。

    fit_r.png
    fit_g.png
    fit_b.png
        R/G/B 三通道拟合曲线。

    residual_r.png
    residual_g.png
    residual_b.png
        R/G/B 三通道相对误差图。


八、数据采集建议
----------------

1. 每个亮度等级至少拍 5 到 10 张图像，取 ROI 平均值。

2. 每个曝光时间都要拍暗场，不能只用一个固定暗场。

3. 标定数据应避开过暗和饱和区域。

   例如：

       8-bit 图像建议保留约 20 到 230 DN
       12-bit 图像建议保留约 300 到 3500 DN
       16-bit 图像建议避开接近 0 和接近满量程的区域

4. 同一个亮度等级下，DN - 曝光时间应接近线性。

5. 同一个亮度等级下，(DN - Dark) / 曝光时间 应基本稳定。

6. R/G/B 三通道要分别检查饱和。红通道受 650 nm 截止滤光片影响较大，不能用其他通道代替。


九、标定完成后如何使用
----------------------

假设 calibration_coefficients.json 中给出 R 通道公式：

    L_eff_r = a_r * X_r + b_r

实际图像反算时：

    X_r = (DN_r - Dark_r) / exposure_s
    L_eff_r = a_r * X_r + b_r

G、B 通道同理。

如果使用 poly 或 logpoly 模型，按 calibration_report.md 或 calibration_coefficients.json 中的公式计算。


十、常见错误
------------

1. 忘记扣暗场

   会导致低亮度区域误差很大。

2. 曝光时间单位填错

   exposure_ms 是毫秒。如果你使用 exposure_s，则单位是秒。

3. 使用 JPEG 或 sRGB 图像

   JPEG/sRGB 通常经过 Gamma 和 ISP 处理，不适合严格辐射标定。

4. 自动曝光或自动白平衡未关闭

   会导致同一亮度等级下 DN 不稳定。

5. 没有考虑滤光片

   650 nm 截止会明显改变红通道有效响应。

6. 把 L_eff 当成单波长辐亮度

   L_eff 是通道加权后的等效辐亮度，不是某个固定波长的 L_lambda。
