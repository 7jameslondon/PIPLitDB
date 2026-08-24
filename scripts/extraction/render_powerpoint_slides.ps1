param(
    [Parameter(Mandatory = $true)]
    [string] $InputPath,

    [Parameter(Mandatory = $true)]
    [string] $OutputDirectory,

    [Parameter(Mandatory = $true)]
    [string] $SlideNumbersCsv,

    [Parameter(Mandatory = $true)]
    [ValidateRange(4000, 12000)]
    [int] $LongEdgePixels
)

$ErrorActionPreference = "Stop"
$inputFull = [System.IO.Path]::GetFullPath($InputPath)
$outputFull = [System.IO.Path]::GetFullPath($OutputDirectory)

if (-not [System.IO.File]::Exists($inputFull)) {
    throw "Input presentation does not exist."
}
if ([System.IO.Path]::GetExtension($inputFull).ToLowerInvariant() -ne ".pptx") {
    throw "Only macro-free PPTX presentations may be rendered."
}
if (-not [System.IO.Directory]::Exists($outputFull)) {
    throw "Renderer output directory does not exist."
}
if ([System.IO.Directory]::EnumerateFileSystemEntries($outputFull).GetEnumerator().MoveNext()) {
    throw "Renderer output directory must be empty."
}

$slideNumbers = @(
    $SlideNumbersCsv.Split(",", [System.StringSplitOptions]::RemoveEmptyEntries) |
        ForEach-Object {
            $number = 0
            if (-not [int]::TryParse($_, [ref] $number) -or $number -lt 1) {
                throw "Slide numbers must be positive integers."
            }
            $number
        }
)
if ($slideNumbers.Count -eq 0 -or @($slideNumbers | Sort-Object -Unique).Count -ne $slideNumbers.Count) {
    throw "At least one unique slide number is required."
}

$application = $null
$presentation = $null
$slides = $null
$records = [System.Collections.Generic.List[object]]::new()

try {
    $application = New-Object -ComObject PowerPoint.Application
    # msoAutomationSecurityForceDisable = 3; ppAlertsNone = 1.
    $application.AutomationSecurity = 3
    $application.DisplayAlerts = 1

    # ReadOnly = msoTrue (-1), Untitled = msoFalse (0), WithWindow = msoFalse (0).
    $presentation = $application.Presentations.Open($inputFull, -1, 0, 0)
    $slides = $presentation.Slides
    $slideCount = [int] $slides.Count
    foreach ($slideNumber in $slideNumbers) {
        if ($slideNumber -gt $slideCount) {
            throw "Requested slide number is outside the presentation."
        }
    }

    $slideWidth = [double] $presentation.PageSetup.SlideWidth
    $slideHeight = [double] $presentation.PageSetup.SlideHeight
    if ($slideWidth -le 0 -or $slideHeight -le 0) {
        throw "Presentation reports invalid slide dimensions."
    }
    if ($slideWidth -ge $slideHeight) {
        $pixelWidth = $LongEdgePixels
        $pixelHeight = [Math]::Max(1, [int] [Math]::Round($LongEdgePixels * $slideHeight / $slideWidth))
    }
    else {
        $pixelHeight = $LongEdgePixels
        $pixelWidth = [Math]::Max(1, [int] [Math]::Round($LongEdgePixels * $slideWidth / $slideHeight))
    }

    foreach ($slideNumber in $slideNumbers) {
        $filename = "slide-{0:D3}.png" -f $slideNumber
        $destination = [System.IO.Path]::Combine($outputFull, $filename)
        if ([System.IO.File]::Exists($destination)) {
            throw "Renderer refuses to overwrite an existing slide image."
        }
        $slide = $null
        try {
            $slide = $slides.Item($slideNumber)
            $slide.Export($destination, "PNG", $pixelWidth, $pixelHeight)
        }
        finally {
            if ($null -ne $slide) {
                [void] [System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($slide)
            }
        }
        if (-not [System.IO.File]::Exists($destination)) {
            throw "PowerPoint did not create the requested slide image."
        }
        $records.Add([ordered] @{
            slide_number = $slideNumber
            filename = $filename
            pixel_width = $pixelWidth
            pixel_height = $pixelHeight
            renderer = "Microsoft PowerPoint"
            renderer_version = [string] $application.Version
        })
    }

    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    [Console]::Out.WriteLine((ConvertTo-Json -InputObject @($records.ToArray()) -Depth 3 -Compress))
}
finally {
    if ($null -ne $slides) {
        [void] [System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($slides)
    }
    if ($null -ne $presentation) {
        try { $presentation.Close() } catch {}
        [void] [System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($presentation)
    }
    if ($null -ne $application) {
        try { $application.Quit() } catch {}
        [void] [System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($application)
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
