# Icône TurboDL : éclair cyan sur carré arrondi sombre (PNG dans ICO, sans dépendance)
import struct, zlib, sys
def png(n):
    bolt=[(0.58,0.10),(0.26,0.56),(0.47,0.56),(0.40,0.90),(0.74,0.42),(0.53,0.42),(0.62,0.10)]
    def inside(x,y):
        c=False; j=len(bolt)-1
        for i in range(len(bolt)):
            xi,yi=bolt[i]; xj,yj=bolt[j]
            if (yi>y)!=(yj>y) and x<(xj-xi)*(y-yi)/(yj-yi)+xi: c=not c
            j=i
        return c
    rows=b""; r=0.2
    for py in range(n):
        row=b"\x00"
        for px in range(n):
            x,y=(px+.5)/n,(py+.5)/n
            dx=max(r-x,0,x-(1-r)); dy=max(r-y,0,y-(1-r))
            if dx*dx+dy*dy>r*r: row+=b"\0\0\0\0"; continue
            if inside(x,y): row+=bytes((34,211,238,255))
            else:
                t=y; row+=bytes((int(13+10*t),int(18+14*t),int(32+26*t),255))
        rows+=row
    ch=lambda t,d: struct.pack(">I",len(d))+t+d+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
    return b"\x89PNG\r\n\x1a\n"+ch(b"IHDR",struct.pack(">IIBBBBB",n,n,8,6,0,0,0))+ch(b"IDAT",zlib.compress(rows,9))+ch(b"IEND",b"")

if __name__ == "__main__":
    # python mkicon.py turbodl.ico [dossier_extension]
    sizes=[16,32,48,256]; imgs=[png(s) for s in sizes]
    out=struct.pack("<HHH",0,1,len(sizes)); off=6+16*len(sizes)
    for s,d in zip(sizes,imgs):
        out+=struct.pack("<BBBBHHII",s%256,s%256,0,0,1,32,len(d),off); off+=len(d)
    open(sys.argv[1],"wb").write(out+b"".join(imgs))

    if len(sys.argv) > 2:
        for n in (16, 32, 48, 128):
            open(f"{sys.argv[2]}/icon{n}.png", "wb").write(png(n))
