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
      float angle = atan(point.y, point.x);
      float radius = length(point);
      float time = uTime * 0.105;

      float bend = sin(angle * 3.0 + time * 1.55) * 0.11 + noise(point * 2.0 + time) * 0.2;
      float cosine = cos(bend);
      float sine = sin(bend);
      vec2 swirl = mat2(cosine, -sine, sine, cosine) * point;
      float contourScale = mix(0.14, 1.0, smoothstep(0.045, 0.22, uSize));
      float contour = (
        noise(vec2(angle * 1.8, time * 0.95)) * 0.032 +
        sin(angle * 7.0 - time) * 0.007
      ) * contourScale;
      float sphere = smoothstep(uSize + contour + 0.014, uSize + contour - 0.016, radius);

      vec2 flowPoint = swirl;
      flowPoint.x += sin(swirl.y * 10.0 + time * 1.15) * 0.055;
      flowPoint.y += cos(swirl.x * 8.0 - time * 0.82) * 0.065;
      float cloud = 0.5;
      cloud += sin(flowPoint.x * 18.0 + flowPoint.y * 4.0 + time * 1.1) * 0.22;
      cloud += sin(dot(flowPoint, vec2(-12.0, 16.0)) - time * 0.72) * 0.17;
      cloud += cos(dot(flowPoint, vec2(22.0, 9.0)) + time * 0.48) * 0.1;
      cloud += sin(dot(flowPoint, vec2(-36.0, 29.0)) + time * 0.84) * 0.055;
      cloud += cos(dot(flowPoint, vec2(41.0, -18.0)) - time * 0.62) * 0.035;
      cloud += (noise(flowPoint * 18.0 + vec2(time * 0.3, -time * 0.2)) - 0.5) * 0.08;
      float pigment = smoothstep(0.12, 0.88, cloud);
      vec3 wash = vec3(0.94, 0.945, 0.94);
      vec3 ink = vec3(0.008, 0.011, 0.012);
      vec3 color = mix(wash, ink, pigment);
      color = mix(color, ink, smoothstep(0.5, 0.9, cloud) * 0.5);
      float grain = hash(gl_FragCoord.xy);
      float alpha = sphere * (0.48 + pigment * 0.5 + grain * 0.012) * uStrength;
      gl_FragColor = vec4(color, alpha);
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
      } else if (mode === "login") {
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
      this.mode = ["landing", "login", "workspace"].includes(mode) ? mode : "landing";
      this.updateTargets(this.mode, immediate);
    }

    setActive(active) {
      this.active = Boolean(active);
      if (this.active) this.needsResize = true;
    }

    randomize() {
      this.seed = Math.random() * 1000;
      this.startedAt = performance.now() - Math.random() * 8000;
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

      const gl = this.gl;
      gl.disable(gl.SCISSOR_TEST);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      const radiusPixels = Math.ceil((this.size + 0.075) * this.canvas.height);
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
      gl.uniform1f(this.locations.time, this.reducedMotion ? 0 : (now - this.startedAt) / 1000);
      gl.uniform2f(this.locations.resolution, this.canvas.width, this.canvas.height);
      gl.uniform2f(this.locations.center, this.center[0], this.center[1]);
      gl.uniform1f(this.locations.size, this.size);
      gl.uniform1f(this.locations.strength, this.strength);
      gl.uniform1f(this.locations.seed, this.seed);
      gl.drawArrays(gl.TRIANGLES, 0, 6);
      gl.disable(gl.SCISSOR_TEST);
    }
  }

  window.HSInkBackground = HSInkBackground;
})();
