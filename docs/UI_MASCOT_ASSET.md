# 首页双小狗插图记录

资源：`src/jiadun/resources/home-puppies.png`。内置 imagegen 生成，透明 PNG；用于首页欢迎区，角色为用户指定的“小白与鸡毛”。依据用户修正，按原图右侧小狗的短圆侧耳和脸型制作。原始参考图未打包。插图不含小熊。未引入运行时图片生成或网络请求。

最终生成提示如下：

```text
Use case: precise-object-edit / background-extraction
Asset type: transparent welcome illustration for a warm charcoal/red-orange desktop application.
Input image 1 is the edit target and exact character identity reference.
User correction: DO NOT redesign the dog's ears. They want exactly the dog in their original image, the warm beige character on the RIGHT holding sword/shield. The previous version wrongly gave both dogs long drooping ears. That must NOT happen.
Primary request: extract the ORIGINAL RIGHT-HAND PUPPY from the image faithfully, preserving its exact face, head shape, SHORT rounded side ear puffs, proportions, fur, expression, red collar, sword and shield; remove the entire landscape. Place this original beige puppy on the RIGHT. Replace the original left bear with a WHITE-FUR version of the SAME ORIGINAL RIGHT-HAND puppy, keeping the SAME SHORT rounded side ear geometry, head and muzzle identity. The white puppy on LEFT holds the original black spear. Exactly TWO puppies, white Xiaobai and beige Jimao.
Invariants: original right beige dog's face, eyes, nose, smiling mouth, short rounded side ears, compact plush proportions, sword and shield must remain faithful to original. Match both puppy heads to that original right dog. Don't invent breed anatomy. Ears are short compact round side puffs level with eyes, they DO NOT extend down to the shoulders, and NO long spaniel-like ears. The white puppy is the same dog recolored white, NOT the original left bear.
Composition: horizontal transparent cutout of both full-body puppies, generous transparent margins around all weapons/feet, comparable sizes and comfortable spacing; preserve the reference dynamic pose and warm coral edge light.
Scene/backdrop: truly transparent, no landscape, rocks, sky, fire, ground or shadows beyond soft tight cutout edge.
Text: none.
Avoid: long hanging ears, drooping ears, enlarged ears, redesigned faces, bear, third character, extra limbs, cropped props, text, logos, watermark.
```
