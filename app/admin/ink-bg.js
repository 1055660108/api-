(function () {
  "use strict";

  const VERTEX_SHADER = `
    attribute vec2 aPosition;
    varying vec2 vUv;
    void main() {
      vUv = aPosition * 0.5 + 0.5;
      gl_Position = vec4(aPosition, 0.0, 1.0);
    }
  `;

  const FRAGMENT_SHADER = `
    precision highp float;
    varying vec2 vUv;
    uniform float uTime;
    uniform vec2 uResolution;
    uniform vec2 uCenter;
    uniform float uSize;
    uniform float uStrength;
    uniform float uSeed;
    uniform float uNight;
    uniform float uVortex;

    float hash(vec2 p) {
      p += vec2(uSeed * 0.0137, uSeed * 0.0191);
      p = fract(p * vec2(123.34, 456.21));
      p += dot(p, p + 45.32);
      return fract(p.x * p.y);
    }

    float noise(vec2 p) {
      vec2 i = floor(p);
      vec2 f = fract(p);
      f = f * f * f * (f * (f * 6.0 - 15.0) + 10.0);
      return mix(
        mix(hash(i), hash(i + vec2(1.0, 0.0)), f.x),
        mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), f.x),
        f.y
      );
    }

    float fbm(vec2 p) {
      float value = 0.0;
      float amplitude = 0.52;
      mat2 rotation = mat2(0.82, -0.57, 0.57, 0.82);
      for (int i = 0; i < 4; i++) {
        value += amplitude * noise(p);
        p = rotation * p * 2.03 + 9.17;
        amplitude *= 0.5;
      }
      return value;
    }

    void main() {
      vec2 point = vUv - uCenter;
      point.x *= uResolution.x / max(uResolution.y, 1.0);
      vec2 screenPoint = point;
      float screenRadius = length(screenPoint);
      float time = uTime * 0.105;
      float radius = length(point);
      float bodyRotation = uVortex * time * 4.8;
      float internalDrift = uVortex * (
        (1.0 - smoothstep(0.0, max(uSize, 0.04), radius)) * 0.34 +
        (noise(point * 11.0 + time * 1.7) - 0.5) * 0.16
      );
      float vortexRotation = bodyRotation + internalDrift;
      float vortexCosine = cos(vortexRotation);
      float vortexSine = sin(vortexRotation);
      point = mat2(vortexCosine, -vortexSine, vortexSine, vortexCosine) * point;
      float angle = atan(point.y, point.x);
      radius = length(point);

      float bend = sin(angle * 2.0 + time * 1.2) * 0.035 + (noise(point * 2.4 + time) - 0.5) * 0.08;
      float cosine = cos(bend);
      float sine = sin(bend);
      vec2 swirl = mat2(cosine, -sine, sine, cosine) * point;
      float contourScale = mix(0.14, 1.0, smoothstep(0.045, 0.22, uSize));
      float surfaceFlow = fbm(point / max(uSize, 0.04) * 1.08 + vec2(time * 0.13, -time * 0.09));
      float contour = (
        (surfaceFlow - 0.5) * 0.034 +
        sin(angle * 4.0 - time * 0.8) * 0.003 +
        (noise(point * 18.0 + vec2(-time * 1.3, time)) - 0.5) * 0.01 * uVortex
      ) * contourScale;
      float sphere = smoothstep(uSize + contour + 0.014, uSize + contour - 0.016, radius);

      float detailScale = mix(21.0, 5.8, smoothstep(0.05, 0.24, uSize));
      vec2 warp = vec2(
        noise(swirl * 3.4 + vec2(time * 0.38, -time * 0.22)),
        noise(swirl * 3.4 + vec2(7.1 - time * 0.27, 3.7 + time * 0.31))
      ) - 0.5;
      vec2 inkPoint = swirl + warp * mix(0.014, 0.072, smoothstep(0.05, 0.24, uSize));
      float flowTime = time * (1.0 + uVortex * 2.8);
      float cloud = fbm(inkPoint * detailScale + vec2(flowTime, -flowTime * 0.66));
      float folds = noise(inkPoint * detailScale * 1.88 - vec2(time * 1.06, time * 0.4));
      float vein = abs(noise(inkPoint * detailScale * 2.64 + vec2(-time * 0.42, time * 0.28)) - 0.5) * 2.0;
      float ring = sin(radius / max(uSize, 0.04) * 12.0 - cloud * 4.2 + time * 0.9) * 0.5 + 0.5;
      float fluidDensity = cloud * 0.72 + folds * 0.15 + ring * 0.025 + vein * 0.025;
      float pigment = smoothstep(0.08, 0.94, fluidDensity);

      vec2 spherePoint = point / max(uSize, 0.04);
      float depth = sqrt(max(0.0, 1.0 - dot(spherePoint, spherePoint)));
      vec3 normal = normalize(vec3(spherePoint.x, -spherePoint.y, depth));
      float light = dot(normal, normalize(vec3(-0.46, -0.52, 0.72))) * 0.5 + 0.5;
      float highlight = smoothstep(0.58, 0.94, light) * depth;
      float movingHighlight = pow(max(0.0, dot(normal, normalize(vec3(sin(time * 5.4) * 0.5 - 0.25, -0.58, 0.78)))), 12.0) * depth * uVortex;
      float shade = 1.0 - smoothstep(0.18, 0.72, light);
      float rim = pow(1.0 - depth, 1.65);

      vec3 wash = vec3(0.86, 0.87, 0.865);
      vec3 ink = vec3(0.008, 0.011, 0.012);
      vec3 color = mix(wash, ink, pigment);
      color = mix(color, vec3(0.965), highlight * (0.1 + (1.0 - pigment) * 0.1));
      color = mix(color, ink, shade * 0.16 + rim * 0.1);
      float causticField = fbm(point / max(uSize, 0.04) * 3.2 + vec2(flowTime * 0.32, -flowTime * 0.24));
      float caustic = smoothstep(0.67, 0.84, causticField) * smoothstep(0.08, 0.88, depth);
      vec3 rainGlass = mix(vec3(0.22, 0.31, 0.33), vec3(0.008, 0.016, 0.019), pigment * 0.5 + shade * 0.12);
      rainGlass = mix(rainGlass, vec3(0.82, 0.91, 0.9), highlight * (0.28 + (1.0 - pigment) * 0.26));
      rainGlass = mix(rainGlass, vec3(0.99, 1.0, 1.0), movingHighlight * 0.62);
      rainGlass = mix(rainGlass, vec3(0.62, 0.77, 0.76), caustic * (0.16 + uVortex * 0.16));
      rainGlass = mix(rainGlass, vec3(0.08, 0.14, 0.15), rim * 0.22);
      color = mix(color, rainGlass, uNight);
      float grain = hash(gl_FragCoord.xy);
      float inkAlpha = 0.72 + pigment * 0.24 + depth * 0.02 + grain * 0.008;
      float waterAlpha = 0.5 + pigment * 0.3 + depth * 0.04 + rim * 0.08 + uVortex * 0.13;
      float alpha = sphere * mix(inkAlpha, waterAlpha, uNight) * uStrength;

      vec2 groundPoint = vec2(
        point.x / max(uSize * 1.04, 0.01),
        (point.y + uSize * 0.98) / max(uSize * 0.22, 0.01)
      );
      float groundShadow = smoothstep(1.0, 0.0, length(groundPoint)) * (1.0 - sphere) * uStrength * 0.12;
      float halo = smoothstep(uSize + 0.095, uSize + 0.012, screenRadius) * (1.0 - sphere) * uNight * 0.09;
      vec3 groundColor = mix(vec3(0.05), vec3(0.36, 0.43, 0.43), uNight);
      vec3 finalColor = mix(groundColor, color, sphere);
      finalColor = mix(finalColor, vec3(0.72, 0.79, 0.79), halo * 0.72);
      gl_FragColor = vec4(finalColor, max(max(alpha, groundShadow), halo));
    }
  `;

  function compileShader(gl, type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      const message = gl.getShaderInfoLog(shader) || "unknown shader error";
      gl.deleteShader(shader);
      throw new Error(message);
    }
    return shader;
  }

  function createProgram(gl) {
    const program = gl.createProgram();
    const vertex = compileShader(gl, gl.VERTEX_SHADER, VERTEX_SHADER);
    const fragment = compileShader(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER);
    gl.attachShader(program, vertex);
    gl.attachShader(program, fragment);
    gl.linkProgram(program);
    gl.deleteShader(vertex);
    gl.deleteShader(fragment);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      const message = gl.getProgramInfoLog(program) || "unknown program error";
      gl.deleteProgram(program);
      throw new Error(message);
    }
    return program;
  }

  class HSInkBackground {
    constructor(canvas, options = {}) {
      this.canvas = canvas;
      this.kind = options.kind === "workspace" ? "workspace" : "entry";
      this.mode = this.kind === "workspace" ? "workspace" : "landing";
      this.active = true;
      this.reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      this.center = [0.5, 0.52];
      this.targetCenter = [0.5, 0.52];
      this.size = 0.3;
      this.targetSize = 0.3;
      this.strength = this.kind === "workspace" ? 0.34 : 1;
      this.targetStrength = this.strength;
      this.seed = Math.random() * 1000;
      this.startedAt = performance.now();
      this.growth = this.kind === "entry" ? 0.42 : 1;
      this.lastSplashAt = 0;
      this.lastWidth = 0;
      this.lastHeight = 0;
      this.needsResize = true;
      this.frame = 0;

      try {
        this.gl = canvas.getContext("webgl", {
          alpha: true,
          antialias: false,
          depth: false,
          stencil: false,
          powerPreference: "high-performance",
          preserveDrawingBuffer: false,
        });
        if (!this.gl) throw new Error("WebGL unavailable");
        this.program = createProgram(this.gl);
        this.prepareGeometry();
        this.cacheLocations();
        this.gl.enable(this.gl.BLEND);
        this.gl.blendFunc(this.gl.SRC_ALPHA, this.gl.ONE_MINUS_SRC_ALPHA);
        this.resizeObserver = new ResizeObserver(() => { this.needsResize = true; });
        this.resizeObserver.observe(canvas.parentElement || canvas);
        this.setMode(this.mode, true);
        this.render = this.render.bind(this);
        this.frame = requestAnimationFrame(this.render);
      } catch (error) {
        canvas.classList.add("ink-canvas-fallback");
        console.warn("ink background disabled", error);
      }
    }

    prepareGeometry() {
      const gl = this.gl;
      const buffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
      this.buffer = buffer;
    }

    cacheLocations() {
      const gl = this.gl;
      this.locations = {
        position: gl.getAttribLocation(this.program, "aPosition"),
        time: gl.getUniformLocation(this.program, "uTime"),
        resolution: gl.getUniformLocation(this.program, "uResolution"),
        center: gl.getUniformLocation(this.program, "uCenter"),
        size: gl.getUniformLocation(this.program, "uSize"),
        strength: gl.getUniformLocation(this.program, "uStrength"),
        seed: gl.getUniformLocation(this.program, "uSeed"),
        night: gl.getUniformLocation(this.program, "uNight"),
        vortex: gl.getUniformLocation(this.program, "uVortex"),
      };
    }

    dimensions() {
      const rect = this.canvas.getBoundingClientRect();
      return { width: Math.max(0, rect.width), height: Math.max(0, rect.height) };
    }

    resize() {
      if (!this.gl) return false;
      const { width, height } = this.dimensions();
      if (width < 2 || height < 2) return false;
      const compact = width < 720;
      const pixelRatio = Math.min(window.devicePixelRatio || 1, compact ? 1.25 : 1.5);
      const renderWidth = Math.max(1, Math.round(width * pixelRatio));
      const renderHeight = Math.max(1, Math.round(height * pixelRatio));
      if (this.canvas.width !== renderWidth || this.canvas.height !== renderHeight) {
        this.canvas.width = renderWidth;
        this.canvas.height = renderHeight;
        this.gl.viewport(0, 0, renderWidth, renderHeight);
      }
      this.lastWidth = width;
      this.lastHeight = height;
      this.needsResize = false;
      this.updateTargets(this.mode, false);
      return true;
    }

    updateTargets(mode, immediate) {
      const width = this.lastWidth || this.canvas.clientWidth || window.innerWidth;
      const height = this.lastHeight || this.canvas.clientHeight || window.innerHeight;
      const compact = width < 720;
      const aspect = width / Math.max(height, 1);
      if (mode === "landing") {
        this.targetCenter = [0.5, compact ? 0.55 : 0.52];
        this.targetSize = Math.min(compact ? 0.27 : 0.268, aspect * 0.43);
        this.targetStrength = 1;
      } else if (mode === "login" || mode === "converging") {
        this.targetCenter = [0.5, compact ? 0.78 : 0.8];
        this.targetSize = Math.min(0.047, aspect * 0.1);
        this.targetStrength = 0.94;
      } else {
        this.targetCenter = [compact ? 0.76 : 0.84, compact ? 0.78 : 0.72];
        this.targetSize = Math.min(compact ? 0.22 : 0.42, aspect * 0.48);
        this.targetStrength = 0.32;
      }
      if (immediate) {
        this.center = [...this.targetCenter];
        this.size = this.targetSize;
        this.strength = this.targetStrength;
      }
    }

    setMode(mode, immediate = false) {
      const previousMode = this.mode;
      this.mode = ["landing", "converging", "login", "workspace"].includes(mode) ? mode : "landing";
      this.updateTargets(this.mode, immediate);
      if (this.kind === "entry" && this.mode === "landing" && previousMode !== "landing") {
        this.growth = this.reducedMotion ? 1 : 0.42;
      }
    }

    setActive(active) {
      this.active = Boolean(active);
      if (this.active) this.needsResize = true;
    }

    randomize() {
      this.seed = Math.random() * 1000;
      this.startedAt = performance.now() - Math.random() * 8000;
      if (this.kind === "entry" && this.mode === "landing") this.growth = this.reducedMotion ? 1 : 0.42;
    }

    containsPoint(clientX, clientY) {
      if (!this.active || this.mode !== "landing" || !this.lastWidth || !this.lastHeight) return false;
      const centerX = this.center[0] * this.lastWidth;
      const centerY = (1 - this.center[1]) * this.lastHeight;
      const radius = this.size * this.lastHeight * Math.max(0.72, this.growth);
      return Math.hypot(clientX - centerX, clientY - centerY) <= radius * 1.06;
    }

    burstAt(clientX, clientY) {
      const now = performance.now();
      if (this.reducedMotion || now - this.lastSplashAt < 180 || !this.containsPoint(clientX, clientY)) return null;
      const centerX = this.center[0] * this.lastWidth;
      const centerY = (1 - this.center[1]) * this.lastHeight;
      const radius = this.size * this.lastHeight * Math.max(0.72, this.growth);
      this.lastSplashAt = now;
      return { x: centerX, y: centerY, radius };
    }

    render(now) {
      this.frame = requestAnimationFrame(this.render);
      if (!this.active || document.hidden || !this.gl || !this.canvas.isConnected) return;
      if (this.needsResize && !this.resize()) return;
      const ease = this.reducedMotion ? 1 : 0.075;
      this.center[0] += (this.targetCenter[0] - this.center[0]) * ease;
      this.center[1] += (this.targetCenter[1] - this.center[1]) * ease;
      this.size += (this.targetSize - this.size) * ease;
      this.strength += (this.targetStrength - this.strength) * ease;
      if (this.mode === "landing") this.growth += (1 - this.growth) * (this.reducedMotion ? 1 : 0.0085);
      else this.growth += (1 - this.growth) * 0.08;

      const timeSeconds = this.reducedMotion ? 0 : (now - this.startedAt) / 1000;
      const breathing = this.mode === "landing" && !this.reducedMotion ? 1 + Math.sin(timeSeconds * 0.42) * 0.018 : 1;
      const displaySize = this.size * this.growth * breathing;

      const gl = this.gl;
      gl.disable(gl.SCISSOR_TEST);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      const radiusPixels = Math.ceil((displaySize + 0.075) * this.canvas.height);
      const centerX = Math.round(this.center[0] * this.canvas.width);
      const centerY = Math.round(this.center[1] * this.canvas.height);
      const left = Math.max(0, centerX - radiusPixels);
      const bottom = Math.max(0, centerY - radiusPixels);
      const right = Math.min(this.canvas.width, centerX + radiusPixels);
      const top = Math.min(this.canvas.height, centerY + radiusPixels);
      gl.enable(gl.SCISSOR_TEST);
      gl.scissor(left, bottom, Math.max(1, right - left), Math.max(1, top - bottom));
      gl.useProgram(this.program);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
      gl.enableVertexAttribArray(this.locations.position);
      gl.vertexAttribPointer(this.locations.position, 2, gl.FLOAT, false, 0, 0);
      gl.uniform1f(this.locations.time, timeSeconds);
      gl.uniform2f(this.locations.resolution, this.canvas.width, this.canvas.height);
      gl.uniform2f(this.locations.center, this.center[0], this.center[1]);
      gl.uniform1f(this.locations.size, displaySize);
      gl.uniform1f(this.locations.strength, this.strength);
      gl.uniform1f(this.locations.seed, this.seed);
      gl.uniform1f(this.locations.night, this.kind === "entry" ? 1 : 0);
      gl.uniform1f(this.locations.vortex, this.kind === "entry" ? 1 : 0);
      gl.drawArrays(gl.TRIANGLES, 0, 6);
      gl.disable(gl.SCISSOR_TEST);
    }
  }

  class HSRainScene {
    constructor(canvas) {
      this.canvas = canvas;
      this.context = canvas.getContext("2d", { alpha: true });
      this.active = true;
      this.mode = "landing";
      this.reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      this.pixelRatio = 1;
      this.width = 0;
      this.height = 0;
      this.wind = -24;
      this.drops = [];
      this.ripples = [];
      this.glassDrops = [];
      this.sprays = [];
      this.orbitDrops = [];
      this.transitionStartedAt = 0;
      this.transitionDuration = 1250;
      this.lastFrameAt = performance.now();
      this.lastRenderAt = 0;
      this.frameInterval = 1000 / 60;
      this.needsResize = true;
      this.staticRendered = false;
      this.atmosphereCanvas = document.createElement("canvas");
      if (!this.context) return;
      this.resizeObserver = new ResizeObserver(() => { this.needsResize = true; });
      this.resizeObserver.observe(canvas.parentElement || canvas);
      this.render = this.render.bind(this);
      this.frame = requestAnimationFrame(this.render);
    }

    dimensions() {
      const rect = this.canvas.getBoundingClientRect();
      return { width: Math.max(1, rect.width || window.innerWidth), height: Math.max(1, rect.height || window.innerHeight) };
    }

    resize() {
      const { width, height } = this.dimensions();
      const compact = width < 720;
      this.pixelRatio = Math.min(window.devicePixelRatio || 1, compact ? 1.3 : 1.5);
      this.canvas.width = Math.max(1, Math.round(width * this.pixelRatio));
      this.canvas.height = Math.max(1, Math.round(height * this.pixelRatio));
      this.canvas.style.width = `${width}px`;
      this.canvas.style.height = `${height}px`;
      this.context.setTransform(this.pixelRatio, 0, 0, this.pixelRatio, 0, 0);
      this.width = width;
      this.height = height;
      this.needsResize = false;
      this.buildDrops();
      this.buildAtmosphere();
    }

    buildDrops() {
      const compact = this.width < 720;
      const count = compact ? 124 : Math.min(214, Math.max(162, Math.round(this.width * this.height / 7600)));
      this.drops = Array.from({ length: count }, () => this.createDrop(true));
      const glassCount = compact ? 44 : Math.min(78, Math.max(58, Math.round(this.width * this.height / 19000)));
      this.glassDrops = Array.from({ length: glassCount }, () => this.createGlassDrop(true));
      const orbitCount = compact ? 20 : 30;
      this.orbitDrops = Array.from({ length: orbitCount }, (_, index) => ({
        angle: index / orbitCount * Math.PI * 2 + Math.random() * 0.3,
        radius: 0.66 + Math.random() * 0.8,
        speed: (0.48 + Math.random() * 0.92) * (index % 2 ? 1 : -1),
        size: 1.2 + Math.random() * 4.2,
        lift: 0.28 + Math.random() * 0.62,
        phase: Math.random() * Math.PI * 2,
      }));
      this.ripples = [];
      this.sprays = [];
      this.staticRendered = false;
    }

    buildAtmosphere() {
      const canvas = this.atmosphereCanvas;
      canvas.width = Math.max(1, Math.round(this.width * this.pixelRatio));
      canvas.height = Math.max(1, Math.round(this.height * this.pixelRatio));
      const context = canvas.getContext("2d", { alpha: true });
      if (!context) return;
      context.setTransform(this.pixelRatio, 0, 0, this.pixelRatio, 0, 0);
      context.clearRect(0, 0, this.width, this.height);
      const horizon = context.createLinearGradient(0, 0, 0, this.height);
      horizon.addColorStop(0, "rgba(32, 43, 45, 0.08)");
      horizon.addColorStop(0.52, "rgba(108, 129, 128, 0.025)");
      horizon.addColorStop(0.82, "rgba(1, 5, 6, 0.02)");
      horizon.addColorStop(1, "rgba(178, 202, 199, 0.035)");
      context.fillStyle = horizon;
      context.fillRect(0, 0, this.width, this.height);
      const glow = context.createRadialGradient(this.width * 0.5, this.height * 0.5, 0, this.width * 0.5, this.height * 0.5, Math.max(this.width, this.height) * 0.56);
      glow.addColorStop(0, "rgba(154, 177, 176, 0.035)");
      glow.addColorStop(0.42, "rgba(83, 102, 103, 0.018)");
      glow.addColorStop(1, "rgba(0, 0, 0, 0)");
      context.fillStyle = glow;
      context.fillRect(0, 0, this.width, this.height);

      const reflection = context.createLinearGradient(0, 0, this.width, this.height);
      reflection.addColorStop(0, "rgba(224, 239, 238, 0.025)");
      reflection.addColorStop(0.28, "rgba(117, 147, 148, 0.008)");
      reflection.addColorStop(0.5, "rgba(235, 245, 243, 0.035)");
      reflection.addColorStop(0.72, "rgba(88, 116, 118, 0.008)");
      reflection.addColorStop(1, "rgba(215, 232, 230, 0.022)");
      context.fillStyle = reflection;
      context.fillRect(0, 0, this.width, this.height);
    }

    createDrop(initial = false) {
      const depth = Math.random();
      const speed = 460 + depth * 1260;
      const y = initial ? Math.random() * this.height : -40 - Math.random() * this.height * 0.28;
      return {
        x: Math.random() * (this.width + 180) - 90,
        y,
        impactY: initial
          ? Math.min(this.height * 0.98, y + this.height * (0.04 + Math.random() * 0.32))
          : this.height * (0.08 + Math.random() * 0.88),
        depth,
        speed,
        length: 20 + depth * 68 + Math.random() * 24,
        width: 0.42 + depth * 1.62,
        alpha: 0.07 + depth * 0.48,
        layer: Math.min(2, Math.floor(depth * 3)),
        phase: Math.random() * Math.PI * 2,
      };
    }

    createGlassDrop(initial = false) {
      const depth = Math.random();
      const radius = 1.5 + depth * 5.4 + Math.random() * 2.8;
      return {
        x: Math.random() * this.width,
        y: initial ? Math.random() * this.height : -radius * 3,
        radius,
        speed: 4 + depth * 22 + Math.random() * 12,
        trail: radius * (1.4 + Math.random() * 5.5),
        drift: (Math.random() - 0.5) * 2.2,
        phase: Math.random() * Math.PI * 2,
        pause: Math.random() * 2.4,
        depth,
      };
    }

    resetDrop(drop) {
      Object.assign(drop, this.createDrop(false));
    }

    resetGlassDrop(drop) {
      Object.assign(drop, this.createGlassDrop(false));
    }

    setMode(mode) {
      const nextMode = ["landing", "converging", "login"].includes(mode) ? mode : "login";
      if (nextMode === "converging" && this.mode !== "converging") {
        this.transitionStartedAt = performance.now();
        for (const drop of this.drops) {
          drop.gatherX = drop.x;
          drop.gatherY = drop.y;
          drop.gatherAngle = Math.atan2(drop.y - this.height * 0.2, drop.x - this.width * 0.5);
          drop.gatherTurns = 1.4 + Math.random() * 1.8;
        }
      }
      if (nextMode === "landing" && this.mode !== "landing") {
        this.transitionStartedAt = 0;
        this.buildDrops();
      }
      this.mode = nextMode;
      if (this.mode !== "landing") this.sprays = [];
      this.staticRendered = false;
    }

    setActive(active) {
      this.active = Boolean(active);
      if (this.active) {
        this.needsResize = true;
        this.lastFrameAt = performance.now();
        this.lastRenderAt = 0;
      }
    }

    randomize() {
      this.wind = -38 + Math.random() * 58;
      if (this.width) this.buildDrops();
    }

    burstAt(x, y, radius) {
      if (this.reducedMotion || this.mode !== "landing") return;
      const particleCount = this.width < 720 ? 72 : 104;
      const particles = Array.from({ length: particleCount }, (_, index) => {
        const angle = (index / particleCount) * Math.PI * 2 + (Math.random() - 0.5) * 0.42;
        const speed = radius * (1.05 + Math.random() * 2.15);
        return {
          angle,
          vx: Math.cos(angle) * speed * (0.72 + Math.random() * 0.48),
          vy: Math.sin(angle) * speed - radius * (0.2 + Math.random() * 0.62),
          gravity: radius * (2.4 + Math.random() * 1.9),
          size: 1.4 + Math.random() * 5.8,
          delay: Math.random() * 180,
          life: 620 + Math.random() * 720,
          wobble: (Math.random() - 0.5) * radius * 0.24,
        };
      });
      this.sprays.push({ x, y, radius, startedAt: performance.now(), duration: 1680, particles, phase: Math.random() * Math.PI * 2 });
      if (this.sprays.length > 3) this.sprays.shift();
    }

    drawMist(context, now) {
      context.save();
      context.globalAlpha = 0.82 + Math.sin(now * 0.00022) * 0.08;
      context.drawImage(this.atmosphereCanvas, 0, 0, this.width, this.height);
      context.restore();
    }

    drawRain(context, deltaSeconds) {
      context.save();
      context.lineCap = "round";
      context.globalCompositeOperation = "screen";
      const layerColors = ["rgba(109, 137, 141, .13)", "rgba(158, 190, 192, .27)", "rgba(222, 240, 240, .5)"];
      const layerWidths = [0.48, 0.94, 1.62];
      for (let layer = 0; layer < 3; layer += 1) {
        context.beginPath();
        for (const drop of this.drops) {
          if (drop.layer !== layer) continue;
          if (!this.reducedMotion) {
            drop.y += drop.speed * deltaSeconds;
            drop.x += this.wind * deltaSeconds * (0.32 + drop.depth * 0.68);
          }
          const slant = this.wind * 0.018 * drop.length;
          const sway = Math.sin(drop.phase + drop.y * 0.008) * (0.8 + drop.depth * 1.6);
          context.moveTo(drop.x, drop.y);
          context.quadraticCurveTo(drop.x + slant * 0.42 + sway, drop.y + drop.length * 0.48, drop.x + slant, drop.y + drop.length);
          if (drop.y + drop.length >= drop.impactY || drop.x < -140 || drop.x > this.width + 140) {
            if (drop.x >= -20 && drop.x <= this.width + 20) this.spawnImpact(drop.x + slant, drop.impactY, drop.depth);
            this.resetDrop(drop);
          }
        }
        context.strokeStyle = layerColors[layer];
        context.lineWidth = layerWidths[layer];
        context.shadowColor = layer === 2 ? "rgba(219, 240, 239, .22)" : "transparent";
        context.shadowBlur = layer === 2 ? 3.2 : 0;
        context.stroke();
      }
      context.beginPath();
      for (const drop of this.drops) {
        if (drop.depth < 0.72) continue;
        const slant = this.wind * 0.018 * drop.length;
        context.moveTo(drop.x + slant * 0.58, drop.y + drop.length * 0.58);
        context.lineTo(drop.x + slant, drop.y + drop.length);
      }
      context.strokeStyle = "rgba(246, 252, 251, .34)";
      context.lineWidth = 0.52;
      context.shadowColor = "rgba(210, 238, 238, .28)";
      context.shadowBlur = 2.4;
      context.stroke();
      context.restore();
    }

    spawnImpact(x, y, depth) {
      if (this.ripples.length >= 54 || Math.random() > 0.72 + depth * 0.24) return;
      this.ripples.push({
        x: Math.max(0, Math.min(this.width, x)),
        y: Math.max(0, Math.min(this.height, y)),
        age: 0,
        duration: 0.38 + Math.random() * 0.42,
        size: 5 + depth * 15,
        spark: 3 + Math.floor(depth * 5),
        phase: Math.random() * Math.PI * 2,
      });
    }

    drawRipples(context, deltaSeconds) {
      context.save();
      this.ripples = this.ripples.filter((ripple) => {
        ripple.age += deltaSeconds;
        const progress = ripple.age / ripple.duration;
        if (progress >= 1) return false;
        context.beginPath();
        context.ellipse(ripple.x, ripple.y, ripple.size * (0.28 + progress * 1.25), ripple.size * (0.2 + progress * 0.78), ripple.phase * 0.08, 0, Math.PI * 2);
        context.strokeStyle = `rgba(220, 237, 236, ${(1 - progress) * 0.42})`;
        context.lineWidth = 0.65 + (1 - progress) * 0.5;
        context.stroke();
        context.beginPath();
        for (let index = 0; index < ripple.spark; index += 1) {
          const angle = ripple.phase + index / ripple.spark * Math.PI * 2;
          const inner = ripple.size * (0.15 + progress * 0.28);
          const outer = ripple.size * (0.35 + Math.sin(Math.PI * progress) * (0.7 + index % 3 * 0.16));
          context.moveTo(ripple.x + Math.cos(angle) * inner, ripple.y + Math.sin(angle) * inner);
          context.lineTo(ripple.x + Math.cos(angle) * outer, ripple.y + Math.sin(angle) * outer);
        }
        context.strokeStyle = `rgba(236, 248, 247, ${(1 - progress) * 0.34})`;
        context.lineWidth = 0.6;
        context.stroke();
        return true;
      });
      context.restore();
    }

    drawGlassSurface(context, deltaSeconds, now) {
      const time = now / 1000;
      context.save();
      context.lineCap = "round";
      context.globalCompositeOperation = "screen";
      for (const drop of this.glassDrops) {
        if (!this.reducedMotion) {
          drop.pause = Math.max(0, drop.pause - deltaSeconds);
          if (!drop.pause) {
            const pulse = 0.72 + Math.sin(time * 1.4 + drop.phase) * 0.28;
            drop.y += drop.speed * pulse * deltaSeconds;
            drop.x += drop.drift * deltaSeconds;
          }
        }
        if (drop.y - drop.trail > this.height + drop.radius * 4) {
          this.resetGlassDrop(drop);
          continue;
        }
        const moving = drop.pause <= 0;
        const trailLength = moving ? drop.trail * (0.72 + drop.depth * 0.48) : drop.radius * 0.8;
        context.beginPath();
        context.moveTo(drop.x, drop.y - trailLength);
        context.quadraticCurveTo(drop.x - drop.drift * 1.8, drop.y - trailLength * 0.46, drop.x, drop.y - drop.radius * 0.45);
        context.strokeStyle = `rgba(139, 178, 180, ${0.055 + drop.depth * 0.09})`;
        context.lineWidth = Math.max(0.7, drop.radius * 0.42);
        context.stroke();

        context.beginPath();
        context.ellipse(drop.x, drop.y, drop.radius * 0.78, drop.radius * 1.12, drop.drift * 0.05, 0, Math.PI * 2);
        context.fillStyle = `rgba(118, 157, 160, ${0.055 + drop.depth * 0.11})`;
        context.fill();
        context.strokeStyle = `rgba(218, 238, 237, ${0.16 + drop.depth * 0.22})`;
        context.lineWidth = 0.55;
        context.stroke();

        context.beginPath();
        context.ellipse(drop.x - drop.radius * 0.22, drop.y - drop.radius * 0.32, Math.max(0.4, drop.radius * 0.16), Math.max(0.6, drop.radius * 0.3), -0.34, 0, Math.PI * 2);
        context.fillStyle = `rgba(250, 255, 254, ${0.3 + drop.depth * 0.32})`;
        context.fill();
      }

      const sheen = context.createLinearGradient(0, 0, this.width, 0);
      sheen.addColorStop(0, "rgba(218, 238, 237, 0.012)");
      sheen.addColorStop(0.32, "rgba(236, 247, 245, 0.035)");
      sheen.addColorStop(0.48, "rgba(120, 153, 155, 0.006)");
      sheen.addColorStop(0.74, "rgba(231, 244, 242, 0.025)");
      sheen.addColorStop(1, "rgba(190, 216, 215, 0.009)");
      context.fillStyle = sheen;
      context.fillRect(0, 0, this.width, this.height);
      context.restore();
    }

    loginTarget() {
      const compact = this.width < 720;
      return {
        x: this.width * 0.5,
        y: this.height * (compact ? 0.22 : 0.2),
        radius: Math.max(compact ? 26 : 32, this.height * 0.047),
      };
    }

    drawConvergence(context, now) {
      const target = this.loginTarget();
      const rawProgress = Math.min(1, Math.max(0, (now - this.transitionStartedAt) / this.transitionDuration));
      const progress = rawProgress * rawProgress * (3 - 2 * rawProgress);
      const fade = Math.max(0.08, 1 - rawProgress * 0.82);
      const layerColors = ["rgba(142, 169, 170, .16)", "rgba(184, 209, 209, .29)", "rgba(226, 241, 240, .48)"];
      context.save();
      context.lineCap = "round";
      for (let layer = 0; layer < 3; layer += 1) {
        context.beginPath();
        for (const drop of this.drops) {
          if (drop.layer !== layer) continue;
          const originX = Number.isFinite(drop.gatherX) ? drop.gatherX : drop.x;
          const originY = Number.isFinite(drop.gatherY) ? drop.gatherY : drop.y;
          const startRadius = Math.hypot(originX - target.x, originY - target.y);
          const startAngle = Number.isFinite(drop.gatherAngle) ? drop.gatherAngle : Math.atan2(originY - target.y, originX - target.x);
          const angle = startAngle + progress * Math.PI * 2 * (drop.gatherTurns || 2);
          const radius = startRadius * Math.pow(1 - progress, 1.08) + target.radius * 0.58 * Math.sin(progress * Math.PI);
          const x = target.x + Math.cos(angle) * radius;
          const y = target.y + Math.sin(angle) * radius * (0.68 + drop.depth * 0.12);
          const tangentX = -Math.sin(angle) * drop.length * (0.18 + fade * 0.42);
          const tangentY = Math.cos(angle) * drop.length * (0.18 + fade * 0.42);
          context.moveTo(x - tangentX, y - tangentY);
          context.lineTo(x, y);
        }
        context.strokeStyle = layerColors[layer];
        context.globalAlpha = fade;
        context.lineWidth = 0.55 + layer * 0.42;
        context.stroke();
      }
      context.restore();
      this.drawWaterOrbit(context, now, Math.min(1, rawProgress * 1.7));
    }

    drawWaterOrbit(context, now, strength = 1) {
      if (strength <= 0) return;
      const target = this.loginTarget();
      const time = now / 1000;
      context.save();
      context.lineCap = "round";

      context.beginPath();
      for (let index = 0; index < 14; index += 1) {
        const phase = (time * (0.62 + index % 3 * 0.08) + index / 14) % 1;
        const x = target.x + Math.sin(index * 2.73 + time * 1.3) * target.radius * (0.62 + index % 4 * 0.34);
        const y = target.y - target.radius * 2.3 + phase * target.radius * 4.7;
        context.moveTo(x, y - 4 - index % 4);
        context.lineTo(x - 1.2, y + 7 + index % 5);
      }
      context.strokeStyle = `rgba(164, 199, 200, ${0.18 * strength})`;
      context.lineWidth = 0.75;
      context.stroke();

      for (let index = 0; index < 4; index += 1) {
        const rotation = time * (0.7 + index * 0.17) * (index % 2 ? -1 : 1) + index * 0.9;
        context.beginPath();
        context.ellipse(target.x, target.y, target.radius * (1.05 + index * 0.22), target.radius * (0.3 + index * 0.055), rotation, 0.22, Math.PI * 1.62);
        context.strokeStyle = `rgba(170, 211, 211, ${(0.12 - index * 0.015) * strength})`;
        context.lineWidth = 0.65 + (3 - index) * 0.16;
        context.stroke();
      }

      for (const drop of this.orbitDrops) {
        const angle = drop.angle + time * drop.speed + Math.sin(time * 0.9 + drop.phase) * 0.24;
        const distance = target.radius * drop.radius * (1 + Math.sin(time * 1.7 + drop.phase) * 0.08);
        const x = target.x + Math.cos(angle) * distance * 1.42;
        const y = target.y + Math.sin(angle) * distance * drop.lift;
        const depth = Math.sin(angle) * 0.5 + 0.5;
        const size = drop.size * (0.68 + depth * 0.62) * strength;
        context.beginPath();
        context.arc(x, y, Math.max(0.4, size), 0, Math.PI * 2);
        context.fillStyle = `rgba(129, 177, 179, ${(0.2 + depth * 0.22) * strength})`;
        context.fill();
        context.beginPath();
        context.arc(x - size * 0.28, y - size * 0.34, Math.max(0.3, size * 0.22), 0, Math.PI * 2);
        context.fillStyle = `rgba(244, 251, 250, ${(0.32 + depth * 0.36) * strength})`;
        context.fill();
      }
      context.restore();
    }

    drawSprays(context, now) {
      if (!this.sprays.length) return;
      context.save();
      context.lineCap = "round";
      context.globalCompositeOperation = "screen";
      this.sprays = this.sprays.filter((spray) => {
        const elapsed = now - spray.startedAt;
        if (elapsed >= spray.duration) return false;

        const sheetProgress = Math.min(1, elapsed / 560);
        const sheetFade = Math.max(0, 1 - elapsed / 760);
        if (sheetFade > 0) {
          const sheetRadius = spray.radius * (0.22 + sheetProgress * 1.08);
          const sheet = context.createRadialGradient(spray.x, spray.y, sheetRadius * 0.08, spray.x, spray.y, sheetRadius);
          sheet.addColorStop(0, `rgba(229, 244, 244, ${sheetFade * 0.2})`);
          sheet.addColorStop(0.48, `rgba(124, 177, 181, ${sheetFade * 0.15})`);
          sheet.addColorStop(0.82, `rgba(200, 231, 231, ${sheetFade * 0.1})`);
          sheet.addColorStop(1, "rgba(180, 219, 220, 0)");
          context.beginPath();
          context.arc(spray.x, spray.y, sheetRadius, 0, Math.PI * 2);
          context.fillStyle = sheet;
          context.fill();
          for (let jet = 0; jet < 9; jet += 1) {
            const angle = spray.phase + jet / 9 * Math.PI * 2 + Math.sin(jet * 2.7) * 0.18;
            const inner = sheetRadius * 0.34;
            const outer = sheetRadius * (0.88 + (jet % 3) * 0.16);
            context.beginPath();
            context.moveTo(spray.x + Math.cos(angle) * inner, spray.y + Math.sin(angle) * inner);
            context.quadraticCurveTo(
              spray.x + Math.cos(angle + 0.16) * outer * 0.68,
              spray.y + Math.sin(angle + 0.16) * outer * 0.68,
              spray.x + Math.cos(angle) * outer,
              spray.y + Math.sin(angle) * outer,
            );
            context.strokeStyle = `rgba(212, 237, 237, ${sheetFade * 0.24})`;
            context.lineWidth = 0.7 + (jet % 2) * 0.45;
            context.stroke();
          }
        }

        for (const particle of spray.particles) {
          const localElapsed = elapsed - particle.delay;
          if (localElapsed <= 0 || localElapsed >= particle.life) continue;
          const progress = localElapsed / particle.life;
          const seconds = localElapsed / 1000;
          const visibility = Math.sin(Math.PI * progress) * (1 - progress * 0.22);
          const x = spray.x + particle.vx * seconds + Math.sin(progress * Math.PI * 2 + particle.angle) * particle.wobble * progress;
          const y = spray.y + particle.vy * seconds + particle.gravity * seconds * seconds * 0.5;
          const velocityY = particle.vy + particle.gravity * seconds;
          const travelAngle = Math.atan2(velocityY, particle.vx);
          const tail = particle.size * (2.2 + (1 - progress) * 3.8);

          context.beginPath();
          context.moveTo(x - Math.cos(travelAngle) * tail, y - Math.sin(travelAngle) * tail);
          context.lineTo(x, y);
          context.strokeStyle = `rgba(181, 220, 222, ${visibility * 0.56})`;
          context.lineWidth = Math.max(0.7, particle.size * 0.32);
          context.shadowColor = `rgba(203, 235, 235, ${visibility * 0.32})`;
          context.shadowBlur = Math.min(5, particle.size * 0.7);
          context.stroke();

          context.beginPath();
          context.ellipse(x, y, particle.size * 0.68, particle.size * (1.05 + (1 - progress) * 0.62), travelAngle - Math.PI * 0.5, 0, Math.PI * 2);
          context.fillStyle = `rgba(132, 185, 189, ${visibility * 0.62})`;
          context.fill();
          context.strokeStyle = `rgba(231, 246, 245, ${visibility * 0.4})`;
          context.lineWidth = 0.5;
          context.stroke();
          context.beginPath();
          context.ellipse(x - particle.size * 0.2, y - particle.size * 0.34, Math.max(0.3, particle.size * 0.16), Math.max(0.45, particle.size * 0.28), travelAngle, 0, Math.PI * 2);
          context.fillStyle = `rgba(249, 255, 254, ${visibility * 0.62})`;
          context.fill();
        }

        const pulseWindow = Math.min(elapsed, 1550);
        for (let pulse = 0; pulse < 4; pulse += 1) {
          const pulseAge = pulseWindow - pulse * 360;
          if (pulseAge <= 0 || pulseAge >= 520) continue;
          const progress = pulseAge / 520;
          context.beginPath();
          context.ellipse(spray.x, spray.y, spray.radius * (0.48 + progress * 0.82), spray.radius * (0.16 + progress * 0.28), pulse * 0.37, 0, Math.PI * 2);
          context.strokeStyle = `rgba(207, 233, 233, ${(1 - progress) * 0.26})`;
          context.lineWidth = 0.8;
          context.stroke();
        }
        return true;
      });
      context.restore();
    }

    render(now) {
      this.frame = requestAnimationFrame(this.render);
      if (!this.context || !this.active || document.hidden || !this.canvas.isConnected) return;
      if (this.needsResize) this.resize();
      if (this.reducedMotion && this.staticRendered) return;
      if (!this.reducedMotion && now - this.lastRenderAt < this.frameInterval) return;
      const deltaSeconds = Math.min(0.04, Math.max(0.001, (now - this.lastFrameAt) / 1000));
      this.lastFrameAt = now;
      this.lastRenderAt = now;
      const context = this.context;
      context.clearRect(0, 0, this.width, this.height);
      if (this.mode === "landing") {
        this.drawMist(context, now);
        this.drawRain(context, deltaSeconds);
        this.drawRipples(context, deltaSeconds);
        this.drawSprays(context, now);
        this.drawGlassSurface(context, deltaSeconds, now);
      } else if (this.mode === "converging") {
        this.drawMist(context, now);
        this.drawConvergence(context, now);
      } else {
        this.drawWaterOrbit(context, now, 1);
      }
      this.staticRendered = this.reducedMotion;
    }
  }

  window.HSInkBackground = HSInkBackground;
  window.HSRainScene = HSRainScene;
})();
