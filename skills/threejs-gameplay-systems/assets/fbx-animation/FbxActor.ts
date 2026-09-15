import * as THREE from 'three';
import { FBXLoader } from 'three/addons/loaders/FBXLoader.js';
import { clone as cloneSkinned } from 'three/addons/utils/SkeletonUtils.js';

export type RootMotionMode = 'keep' | 'lock-xz' | 'lock-xyz';
export type MaterialMode = 'keep' | 'standard';

export type FbxLoadOptions = {
  /** Skinned mesh FBX (T-pose or first take). */
  url: string;
  /** Extra clip files. Values are FBX URLs; keys become logical clip names. */
  clips?: Record<string, string>;
  /** Fit the wrapper to this visual height in meters. */
  height?: number;
  yaw?: number;
  /** Map basename (lowercase) to a public URL for Unity/Mixamo texture refs. */
  textureAliases?: Record<string, string>;
  /** Default `lock-xz`: keep vertical hop, kill planar root travel. */
  rootMotion?: RootMotionMode;
  /** Default `standard`: Phong to MeshStandardMaterial. */
  materials?: MaterialMode;
  tint?: string;
};

type PreparedClip = {
  name: string;
  clip: THREE.AnimationClip;
};

const ROOT_BONE_KEYS = new Set([
  'root',
  'hips',
  'hip',
  'pelvis',
  'armature',
  'mixamorighips',
]);

export class FbxRig {
  readonly clipNames: string[];

  private constructor(
    private readonly template: THREE.Group,
    private readonly clips: PreparedClip[],
    private readonly options: FbxLoadOptions,
  ) {
    this.clipNames = clips.map((entry) => entry.name);
  }

  static async load(options: FbxLoadOptions): Promise<FbxRig> {
    const loader = createFbxLoader(options.textureAliases);
    const template = await loader.loadAsync(options.url);
    prepareSkinnedGraph(template, options);

    const prepared: PreparedClip[] = [];
    for (const clip of pickShallowClips(template.animations)) {
      prepared.push(prepareClip(clip, logicalClipName(clip.name), options.rootMotion ?? 'lock-xz'));
    }

    for (const [name, url] of Object.entries(options.clips ?? {})) {
      const file = await loader.loadAsync(url);
      const take = pickShallowClips(file.animations)[0] ?? file.animations[0];
      if (!take) {
        console.warn(`[FbxRig] no clips in ${url} (alias ${name})`);
        disposeGraph(file);
        continue;
      }
      prepared.push(prepareClip(take, name, options.rootMotion ?? 'lock-xz'));
      disposeGraph(file);
    }

    if (prepared.length === 0) {
      console.warn(`[FbxRig] ${options.url} loaded with 0 clips — body will stay in bind pose`);
    } else {
      console.info(`[FbxRig] clips: ${prepared.map((entry) => `${entry.name} ${entry.clip.duration.toFixed(2)}s/${entry.clip.tracks.length}tr`).join(', ')}`);
    }

    return new FbxRig(template, prepared, options);
  }

  stamp(name = 'fbx-actor'): FbxActor {
    const root = cloneSkinned(this.template) as THREE.Group;
    root.traverse((child) => {
      if (!(child instanceof THREE.Mesh)) return;
      const material = child.material;
      if (Array.isArray(material)) child.material = material.map((entry) => entry.clone());
      else if (material) child.material = material.clone();
    });
    const bound = this.clips.map((entry) => ({
      name: entry.name,
      clip: bindClipToGraph(entry.clip.clone(), root),
    }));
    return new FbxActor(root, bound, name, this.options);
  }
}

export class FbxActor {
  readonly group = new THREE.Group();
  readonly mixer: THREE.AnimationMixer;
  private readonly actions = new Map<string, THREE.AnimationAction>();
  private currentName: string | undefined;

  constructor(
    readonly root: THREE.Group,
    clips: PreparedClip[],
    name: string,
    options: FbxLoadOptions,
  ) {
    this.group.name = name;
    this.group.add(root);
    if (options.yaw) this.group.rotation.y = options.yaw;
    fitActorHeight(this.group, options.height);

    this.mixer = new THREE.AnimationMixer(root);
    for (const entry of clips) {
      if (entry.clip.tracks.length === 0) {
        console.warn(`[FbxActor] ${entry.name} bound 0 tracks — bone paths did not match`);
        continue;
      }
      this.actions.set(entry.name, this.mixer.clipAction(entry.clip));
    }
  }

  get current(): string | undefined {
    return this.currentName;
  }

  get clipNames(): string[] {
    return [...this.actions.keys()];
  }

  play(name: string, fadeSeconds = 0.18): boolean {
    const found = this.resolveAction(name);
    if (!found) return false;
    const [clipName, action] = found;
    if (this.currentName === clipName && action.isRunning()) return true;
    action.setLoop(THREE.LoopRepeat, Infinity);
    action.clampWhenFinished = false;
    this.crossTo(action, fadeSeconds);
    this.currentName = clipName;
    return true;
  }

  playOnce(name: string, fadeSeconds = 0.12): Promise<boolean> {
    const found = this.resolveAction(name);
    if (!found) return Promise.resolve(false);
    const [, action] = found;
    action.setLoop(THREE.LoopOnce, 1);
    action.clampWhenFinished = true;
    return new Promise((resolve) => {
      const onFinished = (event: { action: THREE.AnimationAction }) => {
        if (event.action !== action) return;
        this.mixer.removeEventListener('finished', onFinished);
        resolve(true);
      };
      this.mixer.addEventListener('finished', onFinished);
      this.crossTo(action, fadeSeconds);
      this.currentName = found[0];
    });
  }

  update(deltaSeconds: number): void {
    this.mixer.update(deltaSeconds);
  }

  debugState(): { clips: string[]; current?: string; time: number } {
    return { clips: this.clipNames, current: this.currentName, time: this.mixer.time };
  }

  dispose(): void {
    this.mixer.stopAllAction();
    this.mixer.uncacheRoot(this.root);
  }

  private resolveAction(name: string): [string, THREE.AnimationAction] | undefined {
    const exact = this.actions.get(name);
    if (exact) return [name, exact];
    const lowered = name.toLowerCase();
    for (const [clipName, action] of this.actions) {
      if (clipName.toLowerCase() === lowered || clipName.toLowerCase().includes(lowered)) {
        return [clipName, action];
      }
    }
    console.warn(`[FbxActor] no clip matching ${name}; have ${this.clipNames.join(', ') || '(none)'}`);
    return undefined;
  }

  private crossTo(action: THREE.AnimationAction, fadeSeconds: number): void {
    const previous = this.currentName ? this.actions.get(this.currentName) : undefined;
    if (previous && previous !== action && fadeSeconds > 0) {
      action.reset().fadeIn(fadeSeconds).play();
      previous.fadeOut(fadeSeconds);
      return;
    }
    this.mixer.stopAllAction();
    action.reset().play();
  }
}

function disposeGraph(root: THREE.Object3D): void {
  root.traverse((object) => {
    const mesh = object as THREE.Mesh;
    mesh.geometry?.dispose();
  });
}

export function createFbxLoader(textureAliases?: Record<string, string>): FBXLoader {
  const manager = new THREE.LoadingManager();
  if (textureAliases && Object.keys(textureAliases).length > 0) {
    const aliases = Object.fromEntries(
      Object.entries(textureAliases).map(([key, value]) => [key.toLowerCase(), value]),
    );
    manager.setURLModifier((url) => {
      const name = url.split(/[\\/]/).pop()?.split('?')[0]?.toLowerCase() ?? url.toLowerCase();
      return aliases[name] ?? url;
    });
  }
  return new FBXLoader(manager);
}

export function pickShallowClips(clips: THREE.AnimationClip[]): THREE.AnimationClip[] {
  const byTake = new Map<string, THREE.AnimationClip>();
  for (const clip of clips) {
    const take = clip.name.split('|').pop() ?? clip.name;
    const depth = clip.name.split('|').length;
    const existing = byTake.get(take);
    const existingDepth = existing ? existing.name.split('|').length : Number.POSITIVE_INFINITY;
    if (!existing || depth < existingDepth) byTake.set(take, clip);
  }
  return [...byTake.values()];
}

export function logicalClipName(raw: string): string {
  const take = raw.split('|').pop() ?? raw;
  return take.replace(/_remap$/i, '').replace(/[_\-]+/g, '-').toLowerCase();
}

function prepareClip(clip: THREE.AnimationClip, name: string, rootMotion: RootMotionMode): PreparedClip {
  const next = clip.clone();
  next.name = name;
  if (rootMotion !== 'keep') applyRootMotion(next, rootMotion);
  return { name, clip: next };
}

function applyRootMotion(clip: THREE.AnimationClip, mode: RootMotionMode): void {
  for (const track of clip.tracks) {
    const { node, property } = splitTrackName(track.name);
    if (property !== 'position' || !isRootBone(node)) continue;
    const values = track.values;
    if (values.length < 3) continue;
    const x0 = values[0];
    const y0 = values[1];
    const z0 = values[2];
    for (let i = 0; i < values.length; i += 3) {
      values[i] = x0;
      values[i + 2] = z0;
      if (mode === 'lock-xyz') values[i + 1] = y0;
    }
  }
}

export function bindClipToGraph(clip: THREE.AnimationClip, root: THREE.Object3D): THREE.AnimationClip {
  const bones = new Map<string, string>();
  root.traverse((object) => {
    if (object.name) bones.set(normalizeBoneKey(object.name), object.name);
  });

  const tracks: THREE.KeyframeTrack[] = [];
  for (const track of clip.tracks) {
    const { node, property } = splitTrackName(track.name);
    const target = bones.get(normalizeBoneKey(node));
    if (!target) continue;
    const next = track.clone();
    next.name = `${target}.${property}`;
    tracks.push(next);
  }
  return new THREE.AnimationClip(clip.name, clip.duration, tracks);
}

function prepareSkinnedGraph(root: THREE.Object3D, options: FbxLoadOptions): void {
  root.traverse((child) => {
    if (child instanceof THREE.SkinnedMesh) child.frustumCulled = false;
    if (!(child instanceof THREE.Mesh)) return;
    child.castShadow = true;
    child.receiveShadow = true;
    if ((options.materials ?? 'standard') === 'keep') return;
    const materials = Array.isArray(child.material) ? child.material : [child.material];
    const next = materials.map((material) => toStandardMaterial(material, options.tint));
    child.material = Array.isArray(child.material) ? next : next[0];
  });
}

function toStandardMaterial(material: THREE.Material, tint?: string): THREE.Material {
  if (material instanceof THREE.MeshStandardMaterial || material instanceof THREE.MeshPhysicalMaterial) {
    if (tint && !material.map) material.color.set(tint);
    return material;
  }
  const source = material as THREE.MeshPhongMaterial;
  const next = new THREE.MeshStandardMaterial({
    color: source.color?.clone() ?? new THREE.Color(tint ?? '#c8c2b6'),
    map: source.map ?? null,
    normalMap: source.normalMap ?? null,
    aoMap: source.aoMap ?? null,
    emissive: source.emissive?.clone() ?? new THREE.Color(0x000000),
    emissiveMap: source.emissiveMap ?? null,
    roughness: 0.64,
    metalness: 0.08,
    transparent: source.transparent,
    opacity: source.opacity,
    side: source.side,
    alphaTest: source.alphaTest,
  });
  if (next.map && tint) next.color.set(tint);
  else if (!next.map && tint) next.color.set(tint);
  return next;
}

function fitActorHeight(root: THREE.Object3D, height?: number): void {
  const box = meshBox(root);
  if (!box) return;
  const size = box.getSize(new THREE.Vector3());
  const current = Math.max(size.y, 0.05);
  let scale = 1;
  if (height) scale = height / current;
  else if (current > 8) scale = 0.01;
  if (Math.abs(scale - 1) > 0.001) root.scale.multiplyScalar(scale);
  const grounded = meshBox(root);
  if (!grounded) return;
  const center = grounded.getCenter(new THREE.Vector3());
  root.position.x -= center.x;
  root.position.y -= grounded.min.y;
  root.position.z -= center.z;
}

function meshBox(root: THREE.Object3D): THREE.Box3 | null {
  root.updateMatrixWorld(true);
  const box = new THREE.Box3();
  let found = false;
  root.traverse((child) => {
    if (!(child instanceof THREE.Mesh) || !child.geometry) return;
    if (!child.geometry.boundingBox) child.geometry.computeBoundingBox();
    if (!child.geometry.boundingBox) return;
    const geo = child.geometry.boundingBox.clone().applyMatrix4(child.matrixWorld);
    if (geo.isEmpty()) return;
    if (!found) {
      box.copy(geo);
      found = true;
    } else {
      box.union(geo);
    }
  });
  return found ? box : null;
}

function splitTrackName(name: string): { node: string; property: string } {
  const index = name.lastIndexOf('.');
  if (index < 0) return { node: name, property: '' };
  return { node: name.slice(0, index), property: name.slice(index + 1) };
}

function normalizeBoneKey(name: string): string {
  return (name.split('|').pop() ?? name)
    .replace(/^mixamorig:/i, '')
    .replace(/^mixamorig/i, '')
    .replace(/[\s_\-:]/g, '')
    .toLowerCase();
}

function isRootBone(name: string): boolean {
  const key = normalizeBoneKey(name);
  return ROOT_BONE_KEYS.has(key) || key === 'root';
}
